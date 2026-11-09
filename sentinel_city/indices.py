"""Spectral indices and the rule-based land-cover classifier.

All inputs are surface reflectance (0-1) float32 arrays from the composite.
The thresholds were calibrated by eye against a Sentinel-2 true-colour composite of Delhi
(Sep-Oct 2026): river and ponds, the Ridge forest, Najafgarh farmland, dense residential
blocks and the airport. They are a transparent baseline, not a trained model; other climates
(snow, deserts, tropical cloud) will need re-tuning in CLASS_RULES below.
"""
import numpy as np

# Land-cover classes: id -> (key, label, colour). 0 is "no data".
CLASSES = {
    1: ("water", "Water", "#2f6fdb"),
    2: ("dense_veg", "Dense vegetation / tree cover", "#1f6b2c"),
    3: ("moderate_veg", "Moderate vegetation / crops & parks", "#5fae4e"),
    4: ("sparse_veg", "Sparse vegetation / mixed urban green", "#b5d67a"),
    5: ("built", "Built-up / concrete", "#c8423b"),
    6: ("bare", "Bare soil / undeveloped land", "#d9a55b"),
}

CLASS_RULES = {
    "water_mndwi": -0.05,      # MNDWI above this ...
    "water_ndvi_max": 0.10,    # ... with little vegetation signal
    "water_dark_swir": 0.07,   # turbid / shaded water: very low SWIR1 reflectance
    "dense_ndvi": 0.50,
    "moderate_ndvi": 0.35,
    "sparse_ndvi": 0.22,
    "bare_bsi": 0.09,          # Bare Soil Index above this ...
    "bare_blue_red_max": 0.68,  # ... and soil-like (reddish) colour; Delhi roofs/concrete sit at 0.71-0.83
    "water_nir_max": 0.15,     # water absorbs NIR/SWIR; blue roofs and canopies do not
    "water_swir_max": 0.12,
}


def nd(a, b):
    with np.errstate(divide="ignore", invalid="ignore"):
        d = a + b
        out = np.where(np.abs(d) > 1e-3, (a - b) / d, np.nan)
        return np.clip(out, -1, 1).astype("float32")


def compute_indices(b):
    """b: dict of band arrays B02..B12. Returns dict of index arrays."""
    return {
        "ndvi": nd(b["B08"], b["B04"]),                      # vegetation vigour
        "ndbi": nd(b["B11"], b["B08"]),                      # built-up
        "mndwi": nd(b["B03"], b["B11"]),                     # open water
        "ndwi": nd(b["B03"], b["B08"]),                      # water (McFeeters)
        "bsi": nd(b["B11"] + b["B04"], b["B08"] + b["B02"]),  # bare soil
        "ndmi": nd(b["B08"], b["B11"]),                      # vegetation / surface moisture
        "ndti": nd(b["B04"], b["B03"]),                      # turbidity (on water only)
        "ndci": nd(b["B05"], b["B04"]),                      # chlorophyll-a proxy (on water only)
    }


def classify(b, ix, valid):
    r = CLASS_RULES
    out = np.zeros(valid.shape, "uint8")
    with np.errstate(divide="ignore", invalid="ignore"):
        blue_red = b["B02"] / b["B04"]
    dark = (b["B08"] < r["water_nir_max"]) & (b["B11"] < r["water_swir_max"]) & (ix["ndvi"] < r["water_ndvi_max"])
    water = dark & ((ix["mndwi"] > r["water_mndwi"])
                    | ((b["B11"] < r["water_dark_swir"]) & (ix["ndwi"] > -0.05)))
    ndvi = ix["ndvi"]
    dense = ndvi >= r["dense_ndvi"]
    moderate = (ndvi >= r["moderate_ndvi"]) & ~dense
    bare = (ndvi < r["moderate_ndvi"]) & (ix["bsi"] > r["bare_bsi"]) & (blue_red < r["bare_blue_red_max"])
    sparse = (ndvi >= r["sparse_ndvi"]) & (ndvi < r["moderate_ndvi"]) & ~bare
    built = (ndvi < r["sparse_ndvi"]) & ~bare
    for cid, m in ((5, built), (4, sparse), (6, bare), (3, moderate), (2, dense), (1, water)):
        out[m & valid] = cid
    return out


def water_quality_label(ndti, ndci):
    """Plain-language reading of mean turbidity / chlorophyll proxies over water pixels."""
    if ndti is None:
        return "No open water detected"
    turb = "clear" if ndti < -0.1 else "moderately turbid" if ndti < 0.05 else "highly turbid"
    chl = "low algal signal" if ndci < 0.0 else "moderate algal signal" if ndci < 0.15 else "high algal signal (possible eutrophication)"
    return f"{turb.capitalize()}, {chl}"
