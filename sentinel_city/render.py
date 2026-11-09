"""Turn analysis arrays into Web-Mercator PNG overlays for the map, plus legend metadata."""
import numpy as np
from PIL import Image
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject

from . import indices

# name -> (title, value range, colour stops, description, water_only)
RAMPS = {
    "ndvi": ("Vegetation (NDVI)", (-0.1, 0.8), ["#8a5a2b", "#d8c99b", "#9cc96b", "#2f8f3a", "#0f4d1d"],
             "Higher = denser, healthier vegetation", False),
    "ndbi": ("Built-up index (NDBI)", (-0.35, 0.2), ["#f3efe9", "#e7b7a3", "#d06a55", "#9c2a22"],
             "Higher = more concrete / impervious", False),
    "mndwi": ("Water index (MNDWI)", (-0.6, 0.3), ["#f2efe6", "#a9c7e3", "#3f7fc7", "#0b3c80"],
              "Higher = open water", False),
    "ndmi": ("Moisture (NDMI)", (-0.3, 0.4), ["#a5652a", "#e8d9b5", "#9cc4dd", "#2a6aa8"],
             "Higher = wetter vegetation / surfaces", False),
    "bsi": ("Bare soil index (BSI)", (-0.3, 0.25), ["#eef2ea", "#d9c39b", "#b8864a", "#7a4a1d"],
            "Higher = more exposed soil", False),
    "ndti": ("Water turbidity (NDTI)", (-0.25, 0.15), ["#1c5fa8", "#6ea7c9", "#c9b27a", "#8a5a2b"],
             "On water only. Higher = muddier, more suspended sediment", True),
    "ndci": ("Chlorophyll-a proxy (NDCI)", (-0.1, 0.35), ["#1c5fa8", "#5fb3a8", "#9ccf5a", "#3f8f1c"],
             "On water only. Higher = more algae / eutrophication risk", True),
}


def _hex(c):
    return np.array([int(c[i:i + 2], 16) for i in (1, 3, 5)], "float32")


def _ramp(a, lo, hi, stops):
    t = np.clip((a - lo) / (hi - lo), 0, 1)
    stops = np.stack([_hex(s) for s in stops])
    pos = t * (len(stops) - 1)
    i = np.clip(np.floor(pos).astype(int), 0, len(stops) - 2)
    f = (pos - i)[..., None]
    return (stops[i] * (1 - f) + stops[i + 1] * f).astype("uint8")


def _to_mercator(arr, grid, resampling, dst_nodata):
    dst_crs = "EPSG:3857"
    tr, w, h = calculate_default_transform(grid["crs"], dst_crs, grid["width"], grid["height"],
                                           *_bounds(grid))
    out = np.full((h, w), dst_nodata, dtype=arr.dtype)
    reproject(arr, out, src_transform=grid["transform"], src_crs=grid["crs"], dst_transform=tr, dst_crs=dst_crs,
              resampling=resampling, src_nodata=dst_nodata, dst_nodata=dst_nodata)
    return out, tr, w, h


def _bounds(grid):
    t = grid["transform"]
    return t.c, t.f + t.e * grid["height"], t.c + t.a * grid["width"], t.f


def _latlon_bounds(tr, w, h):
    inv = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform
    x0, y1 = tr.c, tr.f
    x1, y0 = tr.c + tr.a * w, tr.f + tr.e * h
    (lon0, lon1), (lat0, lat1) = inv([x0, x1], [y0, y1])
    return [[lat0, lon0], [lat1, lon1]]


LOSSLESS = {"landcover", "transitions"}   # class maps must keep exact colours


def _save(out_dir, name, rgba):
    """WebP keeps the overlays ~8x smaller than PNG; class maps are lossless, the rest quality 82."""
    Image.fromarray(rgba, "RGBA").save(out_dir / f"layer_{name}.webp", "WEBP",
                                       lossless=name in LOSSLESS, quality=82, method=4)
    return f"layer_{name}.webp"


def render_layers(out_dir, bands, ix, cls, valid, grid):
    layers = {}
    # True colour (RGB composite) with a mild gamma stretch
    rgb = []
    for b in ("B04", "B03", "B02"):
        m, tr, w, h = _to_mercator(np.where(valid, bands[b], np.nan).astype("float32"), grid,
                                   Resampling.bilinear, np.nan)
        rgb.append(m)
    rgb = np.dstack(rgb)
    alpha = np.isfinite(rgb).all(-1)
    img = (np.clip(np.nan_to_num(rgb) / 0.25, 0, 1) ** (1 / 1.6) * 255).astype("uint8")
    bounds = _latlon_bounds(tr, w, h)
    layers["truecolor"] = {"title": "Sentinel-2 true colour", "file": _save(out_dir, "truecolor", np.dstack([img, alpha * 255]).astype("uint8")),
                           "kind": "image"}

    c, _, _, _ = _to_mercator(cls, grid, Resampling.nearest, 0)
    pal = np.zeros((7, 4), "uint8")
    for cid, (_, _, col) in indices.CLASSES.items():
        pal[cid, :3] = _hex(col)
        pal[cid, 3] = 255
    layers["landcover"] = {"title": "Land cover", "file": _save(out_dir, "landcover", pal[c]), "kind": "classes",
                           "classes": [{"id": k, "key": v[0], "label": v[1], "color": v[2]}
                                       for k, v in indices.CLASSES.items()]}

    water = cls == 1
    for name, (title, (lo, hi), stops, desc, water_only) in RAMPS.items():
        mask = water if water_only else valid
        a = np.where(mask, ix[name], np.nan).astype("float32")
        m, _, _, _ = _to_mercator(a, grid, Resampling.nearest if water_only else Resampling.bilinear, np.nan)
        ok = np.isfinite(m)
        rgba = np.zeros(m.shape + (4,), "uint8")
        rgba[..., :3] = _ramp(np.nan_to_num(m), lo, hi, stops)
        rgba[..., 3] = ok * 255
        layers[name] = {"title": title, "file": _save(out_dir, name, rgba), "kind": "ramp", "min": lo, "max": hi,
                        "stops": stops, "description": desc}
    for v in layers.values():
        v["bounds"] = bounds
    return layers


def render_change_layers(out_dir, trans, dndvi, grid, transitions, ndvi_stops, ndvi_range=0.4):
    """Transition classes and NDVI difference as Web-Mercator PNG overlays."""
    t, tr, w, h = _to_mercator(trans, grid, Resampling.nearest, 0)
    pal = np.zeros((max(transitions) + 1, 4), "uint8")
    for k, (_, _, col) in transitions.items():
        pal[k, :3] = _hex(col)
        pal[k, 3] = 255
    bounds = _latlon_bounds(tr, w, h)
    layers = {"transitions": {"title": "Land-cover change", "file": _save(out_dir, "transitions", pal[t]),
                              "kind": "classes", "bounds": bounds,
                              "classes": [{"id": k, "key": v[0], "label": v[1], "color": v[2]}
                                          for k, v in transitions.items()]}}
    m, _, _, _ = _to_mercator(dndvi, grid, Resampling.bilinear, np.nan)
    ok = np.isfinite(m)
    rgba = np.zeros(m.shape + (4,), "uint8")
    rgba[..., :3] = _ramp(np.nan_to_num(m), -ndvi_range, ndvi_range, ndvi_stops)
    # Fade near-zero change so real gains and losses stand out
    rgba[..., 3] = (ok * np.clip(np.abs(np.nan_to_num(m)) / 0.08, 0.15, 1) * 255).astype("uint8")
    layers["ndvi_change"] = {"title": "Vegetation change (NDVI difference)", "file": _save(out_dir, "ndvi_change", rgba),
                             "kind": "ramp", "min": -ndvi_range, "max": ndvi_range, "stops": ndvi_stops,
                             "description": "Brown = vegetation lost, green = vegetation gained", "bounds": bounds}
    return layers
