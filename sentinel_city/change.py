"""Change between two runs of the same city: class transitions, NDVI difference, per-zone change.

Runs at the same resolution share one pixel grid (see analysis.py), so change is a per-pixel
comparison of landcover.tif / indices.tif. If two grids ever differ, run B is resampled onto A.
"""
import json
import logging

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.warp import reproject
from shapely.ops import transform as shp_transform

from . import config, geocode, indices, render, zones as zones_mod

log = logging.getLogger(__name__)

VEG = (2, 3, 4)
# Transition categories (id -> key, label, colour). Changes between vegetation densities are
# not transitions here; they show in the NDVI-change layer instead.
TRANSITIONS = {
    1: ("veg_to_built", "Vegetation to built-up", "#c8423b"),
    2: ("bare_to_built", "Bare soil to built-up", "#7b3f99"),
    3: ("veg_to_bare", "Vegetation cleared to bare soil", "#e08a1e"),
    4: ("to_veg", "Greening (built-up or bare to vegetation)", "#2e9e45"),
    5: ("built_to_bare", "Built-up to bare (demolition or site works)", "#8c8577"),
    6: ("new_water", "New open water", "#2f6fdb"),
    7: ("water_lost", "Water lost", "#7fd1d9"),
}
NDVI_DIFF_STOPS = ["#8a5a2b", "#c9a77a", "#e9e6df", "#8cc58a", "#1f7a3a"]


def _read(path, band=1, ref=None):
    with rasterio.open(path) as src:
        if ref is None or (src.transform == ref["transform"] and src.shape == ref["shape"]):
            return src.read(band), {"transform": src.transform, "shape": src.shape, "crs": src.crs}
        out = np.zeros(ref["shape"], src.dtypes[band - 1])
        reproject(rasterio.band(src, band), out, dst_transform=ref["transform"], dst_crs=ref["crs"],
                  resampling=Resampling.nearest)
        return out, ref


def transition_map(a, b):
    va, vb = np.isin(a, VEG), np.isin(b, VEG)
    t = np.zeros(a.shape, "uint8")
    ok = (a > 0) & (b > 0)
    t[ok & va & (b == 5)] = 1
    t[ok & (a == 6) & (b == 5)] = 2
    t[ok & va & (b == 6)] = 3
    t[ok & np.isin(a, (5, 6)) & vb] = 4
    t[ok & (a == 5) & (b == 6)] = 5
    t[ok & (a != 1) & (b == 1)] = 6
    t[ok & (a == 1) & (b != 1)] = 7
    return t


def compute_change(slug, run_a, run_b, force=False):
    """Compare two stored runs (A earlier, B later). Returns the change summary dict."""
    base = config.RESULTS_DIR / slug
    out = base / "changes" / f"{run_a}__{run_b}"
    if (out / "change.json").exists() and not force:
        return json.loads((out / "change.json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    da, db = base / run_a, base / run_b
    sa, sb = (json.loads((d / "summary.json").read_text()) for d in (da, db))
    ca, ref = _read(da / "landcover.tif")
    cb, _ = _read(db / "landcover.tif", ref=ref)
    nda, _ = _read(da / "indices.tif", 1, ref)
    ndb, _ = _read(db / "indices.tif", 1, ref)
    res = sa["resolution_m"]
    px_km2 = res * res / 1e6
    valid = (ca > 0) & (cb > 0)
    t = transition_map(ca, cb)

    # City-level transition matrix (km²) between the six classes
    m = np.zeros((7, 7), "int64")
    np.add.at(m, (ca[valid], cb[valid]), 1)
    keys = {c: v[0] for c, v in indices.CLASSES.items()}
    matrix = {keys[i]: {keys[j]: round(m[i, j] * px_km2, 3) for j in keys} for i in keys}
    tcounts = np.bincount(t[valid], minlength=8)
    transitions = {v[0]: {"label": v[1], "color": v[2], "area_km2": round(tcounts[k] * px_km2, 2),
                          "percent": round(100 * tcounts[k] / max(1, valid.sum()), 3)}
                   for k, v in TRANSITIONS.items()}
    ndvi_ok = valid & (nda != -32768) & (ndb != -32768)
    dndvi = np.where(ndvi_ok, (ndb.astype("float32") - nda) / 10000, np.nan).astype("float32")

    # Per-zone transitions for every zone set
    city = geocode.geocode_city(sa["city"]["query"])
    to_utm = Transformer.from_crs("EPSG:4326", sa["crs"], always_xy=True).transform
    zone_change = {}
    for key, zs in zones_mod.zone_sets(city, sa["crs"]).items():
        shapes = [(shp_transform(to_utm, z["geometry"]), i + 1) for i, z in enumerate(zs["zones"])]
        lab = rasterize(shapes, out_shape=ref["shape"], transform=ref["transform"], fill=0, dtype="int32")
        n = len(zs["zones"]) + 1
        sel = valid & (lab > 0)
        tz = np.zeros((n, 8), "int64")
        np.add.at(tz, (lab[sel], t[sel]), 1)
        tot = np.bincount(lab[sel], minlength=n)
        ok = ndvi_ok & (lab > 0)
        s = np.bincount(lab[ok], weights=dndvi[ok], minlength=n)
        c = np.bincount(lab[ok], minlength=n)
        zone_change[key] = {}
        for i in range(1, n):
            if tot[i] == 0:
                continue
            row = {v[0]: round(tz[i, k] * px_km2, 3) for k, v in TRANSITIONS.items()}
            row["changed_pct"] = round(100 * tz[i, 1:].sum() / tot[i], 2)
            row["mean_dndvi"] = round(float(s[i] / c[i]), 4) if c[i] else None
            zone_change[key][str(i)] = row

    # Map layers
    grid = {"crs": sa["crs"], "transform": ref["transform"], "width": ref["shape"][1], "height": ref["shape"][0]}
    layers = render.render_change_layers(out, t, dndvi, grid, TRANSITIONS, NDVI_DIFF_STOPS)

    summary = {
        "from": {"run_id": run_a, **sa["period"], "label": sa["period"]["end"]},
        "to": {"run_id": run_b, **sb["period"], "label": sb["period"]["end"]},
        "resolution_m": res,
        "compared_km2": round(valid.sum() * px_km2, 1),
        "changed_km2": round(float((t > 0)[valid].sum() * px_km2), 2),
        "transitions": transitions,
        "matrix_km2": matrix,
        "mean_dndvi": round(float(np.nanmean(dndvi)), 4),
        "zones": zone_change,
        "layers": layers,
    }
    (out / "change.json").write_text(json.dumps(summary))
    return summary
