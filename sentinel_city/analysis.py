"""End-to-end city analysis: boundary -> scenes -> composite -> classes -> stats -> map layers.

Every run is stored under results/<city-slug>/<start>_<end>_<res>m/ and is reproducible:
the pixel grid depends only on the city boundary and resolution, so two runs of the same
city at the same resolution are pixel-aligned. That is what period comparison (step two)
will build on: diff landcover.tif / indices.tif between two run folders.
"""
import json
import logging
import time
from collections import Counter
from datetime import datetime, timezone

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.features import rasterize
from shapely.geometry import mapping
from shapely.ops import transform as shp_transform

from . import config, geocode, imagery, indices, render, zones as zones_mod

log = logging.getLogger(__name__)

INDEX_KEYS = ["ndvi", "ndbi", "mndwi", "bsi", "ndmi", "ndti", "ndci"]
GREEN = (2, 3, 4)


def run_dir(slug, start, end, res):
    return config.RESULTS_DIR / slug / f"{start}_{end}_{res}m"


def _class_breakdown(cls_values, px_area_km2):
    n = cls_values.size
    counts = np.bincount(cls_values, minlength=7)
    out = {}
    for cid, (key, label, color) in indices.CLASSES.items():
        out[key] = {"label": label, "color": color, "pixels": int(counts[cid]),
                    "area_km2": round(counts[cid] * px_area_km2, 2),
                    "percent": round(100 * counts[cid] / max(1, n), 2)}
    return out


def _mean(a):
    a = a[np.isfinite(a)]
    return round(float(a.mean()), 4) if a.size else None


def _water_quality(ix, water_mask, min_px=25):
    if water_mask.sum() < min_px:
        return {"pixels": int(water_mask.sum()), "ndti": None, "ndci": None,
                "label": indices.water_quality_label(None, None)}
    ndti, ndci = _mean(ix["ndti"][water_mask]), _mean(ix["ndci"][water_mask])
    return {"pixels": int(water_mask.sum()), "ndti": ndti, "ndci": ndci,
            "label": indices.water_quality_label(ndti, ndci)}


def _zone_stats(label_raster, n_zones, cls, ix, valid, px_area_km2):
    """Vectorised per-zone class counts and index means via bincount."""
    lab = label_raster[valid]
    c = cls[valid]
    size = n_zones + 1
    counts = np.zeros((size, 7), "int64")
    np.add.at(counts, (lab, c), 1)
    totals = counts[:, 1:].sum(1)
    means = {}
    for k in ("ndvi", "ndbi", "ndmi", "bsi"):
        v = ix[k][valid]
        ok = np.isfinite(v)
        s = np.bincount(lab[ok], weights=v[ok], minlength=size)
        n = np.bincount(lab[ok], minlength=size)
        with np.errstate(invalid="ignore", divide="ignore"):
            means[k] = s / n
    water = c == 1
    wq = {}
    for k in ("ndti", "ndci"):
        v = ix[k][valid]
        ok = water & np.isfinite(v)
        s = np.bincount(lab[ok], weights=v[ok], minlength=size)
        n = np.bincount(lab[ok], minlength=size)
        with np.errstate(invalid="ignore", divide="ignore"):
            wq[k] = np.where(n >= 10, s / np.maximum(n, 1), np.nan)
    return counts, totals, means, wq


def _narrative(city_name, summary):
    lc = summary["landcover"]
    green = summary["metrics"]["green_cover_pct"]
    built = lc["built"]["percent"]
    bare = lc["bare"]["percent"]
    water = lc["water"]["percent"]
    ndvi = summary["metrics"]["mean_ndvi"]
    health = "healthy" if ndvi >= 0.45 else "moderate" if ndvi >= 0.3 else "stressed or sparse"
    parts = [f"{city_name}: {green:.0f}% of the land shows vegetation signal (tree cover {lc['dense_veg']['percent']:.0f}%), "
             f"{built:.0f}% is built-up/concrete, {bare:.0f}% is bare or undeveloped soil and {water:.1f}% is open water."]
    parts.append(f"Average vegetation vigour (NDVI {ndvi:.2f}) is {health} for this period.")
    wq = summary["water_quality"]
    if wq["ndti"] is not None:
        parts.append(f"Open water reads as: {wq['label'].lower()} (turbidity index {wq['ndti']:+.2f}, chlorophyll index {wq['ndci']:+.2f}).")
    z = summary.get("highlights")
    if z:
        parts.append(f"Most built-up {z['set_label'].lower()}: {z['most_built']}; greenest: {z['greenest']}; "
                     f"most bare land: {z['most_bare']}.")
    return " ".join(parts)


def analyze_city(city_name, start=None, end=None, res=config.DEFAULT_RESOLUTION, progress=None, force=False):
    t0 = time.time()

    def p(msg, frac):
        log.info("[%3.0f%%] %s", frac * 100, msg)
        if progress:
            progress(msg, frac)

    if not start or not end:
        start, end = imagery.default_period(end)
    p(f"Finding boundary for {city_name}", 0.01)
    city = geocode.geocode_city(city_name)
    out = run_dir(city["slug"], start, end, res)
    if (out / "summary.json").exists() and not force:
        p("Using cached result", 1.0)
        return json.loads((out / "summary.json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    geom = city["geometry"]
    bbox = geom.bounds

    p(f"Searching Sentinel-2 scenes {start} to {end}", 0.04)
    items, source = imagery.search_scenes(bbox, start, end)
    if not items:
        raise RuntimeError(f"No Sentinel-2 scenes found for {start}..{end}")
    epsg = Counter(i["properties"]["proj:epsg"] for i in items).most_common(1)[0][0]
    crs = f"EPSG:{epsg}"
    grid = imagery.target_grid(bbox, crs, res)
    to_utm = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    geom_utm = shp_transform(to_utm, geom)
    shape_ = (grid["height"], grid["width"])
    aoi = rasterize([geom_utm], out_shape=shape_, transform=grid["transform"]).astype(bool)
    p(f"{len(items)} candidate scenes; selecting clearest dates", 0.08)

    bands, comp_info = imagery.build_composite(items, grid, aoi, p)
    p("Computing indices and land cover", 0.86)
    ix = indices.compute_indices(bands)
    valid = aoi & np.isfinite(bands["B04"]) & np.isfinite(bands["B11"])
    cls = indices.classify(bands, ix, valid)
    px_km2 = (res * res) / 1e6

    land = valid & (cls != 1)
    lc = _class_breakdown(cls[valid], px_km2)
    summary = {
        "city": {"query": city_name, "name": city["name"], "slug": city["slug"],
                 "boundary_source": city["source"], "bbox": bbox,
                 "area_km2": round(float(geom_utm.area / 1e6), 1)},
        "period": {"start": start, "end": end},
        "resolution_m": res,
        "crs": crs,
        "imagery": {"source": source, "collection": "Sentinel-2 L2A (surface reflectance)", **comp_info},
        "landcover": lc,
        "metrics": {
            "green_cover_pct": round(sum(lc[indices.CLASSES[c][0]]["percent"] for c in GREEN), 2),
            "tree_cover_pct": lc["dense_veg"]["percent"],
            "impervious_pct": lc["built"]["percent"],
            "mean_ndvi": _mean(ix["ndvi"][land]),
            "mean_ndbi": _mean(ix["ndbi"][land]),
            "mean_ndmi": _mean(ix["ndmi"][land]),
            "mean_bsi": _mean(ix["bsi"][land]),
            "valid_pixel_pct": round(100 * float(valid.sum() / aoi.sum()), 2),
        },
        "water_quality": _water_quality(ix, valid & (cls == 1)),
        "classification_rules": indices.CLASS_RULES,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    p("Area-wise statistics", 0.89)
    to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    zsets = zones_mod.zone_sets(city, crs)
    summary["zone_sets"] = {}
    for key, zs in zsets.items():
        zlist = zs["zones"]
        shapes = [(shp_transform(to_utm, z["geometry"]), i + 1) for i, z in enumerate(zlist)]
        lab = rasterize(shapes, out_shape=shape_, transform=grid["transform"], fill=0, dtype="int32")
        counts, totals, means, wq = _zone_stats(lab, len(zlist), cls, ix, valid & (lab > 0), px_km2)
        feats, rows = [], []
        for i, z in enumerate(zlist, start=1):
            tot = int(totals[i])
            if tot == 0:
                continue
            pct = {indices.CLASSES[c][0]: round(100 * counts[i, c] / tot, 2) for c in indices.CLASSES}
            props = {
                "zid": i, "name": z["name"], "area_km2": round(tot * px_km2, 2), **{f"pct_{k}": v for k, v in pct.items()},
                "pct_green": round(sum(pct[indices.CLASSES[c][0]] for c in GREEN), 2),
                "dominant": max(pct, key=pct.get),
                **{f"mean_{k}": (None if not np.isfinite(means[k][i]) else round(float(means[k][i]), 4)) for k in means},
                **{f"water_{k}": (None if not np.isfinite(wq[k][i]) else round(float(wq[k][i]), 4)) for k in wq},
            }
            rows.append(props)
            g = z["geometry"].simplify(0.0002 if key != "grid" else 0, preserve_topology=True)
            feats.append({"type": "Feature", "properties": props, "geometry": mapping(g)})
        (out / f"zones_{key}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
        summary["zone_sets"][key] = {"label": zs["label"], "count": len(rows), "file": f"zones_{key}.geojson"}
        if key != "grid" and "highlights" not in summary and rows:
            summary["highlights"] = {
                "set_label": zs["label"].split("(")[0].strip(),
                "most_built": max(rows, key=lambda r: r["pct_built"])["name"],
                "greenest": max(rows, key=lambda r: r["pct_green"])["name"],
                "most_bare": max(rows, key=lambda r: r["pct_bare"])["name"],
            }
    summary["narrative"] = _narrative(city["name"].split(",")[0], summary)

    p("Rendering map layers", 0.93)
    summary["layers"] = render.render_layers(out, bands, ix, cls, valid, grid)
    with open(out / "boundary.geojson", "w") as f:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"name": city["name"]}, "geometry": mapping(geom)}]}, f)

    p("Saving rasters", 0.97)
    prof = {"driver": "GTiff", "height": shape_[0], "width": shape_[1], "crs": crs, "transform": grid["transform"],
            "compress": "deflate", "tiled": True, "blockxsize": 256, "blockysize": 256}
    with rasterio.open(out / "landcover.tif", "w", count=1, dtype="uint8", nodata=0, **prof) as dst:
        dst.write(cls, 1)
        dst.write_colormap(1, {c: tuple(int(v[2][i:i + 2], 16) for i in (1, 3, 5)) + (255,)
                               for c, v in indices.CLASSES.items()})
    with rasterio.open(out / "indices.tif", "w", count=len(INDEX_KEYS), dtype="int16", nodata=-32768,
                       predictor=2, **prof) as dst:
        for i, k in enumerate(INDEX_KEYS, start=1):
            a = np.where(valid & np.isfinite(ix[k]), np.clip(ix[k], -1, 1) * 10000, -32768).astype("int16")
            dst.write(a, i)
            dst.set_band_description(i, k)
    summary["rasters"] = {"landcover": "landcover.tif", "indices": "indices.tif", "index_scale": 1e-4,
                          "index_bands": INDEX_KEYS}
    summary["runtime_s"] = round(time.time() - t0, 1)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    _update_index(city["slug"], out, summary)
    p("Done", 1.0)
    return summary


def _update_index(slug, out, summary):
    from .series import rebuild_index
    rebuild_index()


def point_query(slug, run_id, lat, lon):
    """Land-cover class and index values at a lat/lon for a stored run."""
    d = config.RESULTS_DIR / slug / run_id
    with rasterio.open(d / "landcover.tif") as lc, rasterio.open(d / "indices.tif") as ixf:
        x, y = Transformer.from_crs("EPSG:4326", lc.crs, always_xy=True).transform(lon, lat)
        r, c = lc.index(x, y)
        if not (0 <= r < lc.height and 0 <= c < lc.width):
            return {"inside": False}
        win = ((r, r + 1), (c, c + 1))
        cid = int(lc.read(1, window=win)[0, 0])
        vals = ixf.read(window=win)[:, 0, 0]
        out = {"inside": cid != 0, "class_id": cid}
        if cid:
            key, label, color = indices.CLASSES[cid]
            out.update({"class": key, "label": label, "color": color})
        for k, v in zip(ixf.descriptions, vals):
            out[k] = None if v == -32768 else round(v / 10000, 3)
        return out
