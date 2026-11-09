"""Find Sentinel-2 L2A scenes and build a cloud-masked median composite over an area.

Scene discovery tries the Earth Search STAC API first, and falls back to listing the
public `sentinel-cogs` S3 bucket by MGRS tile when the API is unreachable. Both return
the same STAC item JSON, so the rest of the pipeline does not care which one answered.
"""
import json
import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import mgrs
import numpy as np
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from pyproj import Transformer

from . import config

log = logging.getLogger(__name__)

# Band key -> Earth Search asset name
BANDS = {"B02": "blue", "B03": "green", "B04": "red", "B05": "rededge1", "B08": "nir",
         "B11": "swir16", "B12": "swir22"}
# Scene Classification Layer classes treated as unusable:
# 0 no data, 1 saturated, 3 cloud shadow, 8/9 cloud medium/high, 10 cirrus, 11 snow
SCL_INVALID = (0, 1, 3, 8, 9, 10, 11)


# --------------------------------------------------------------------------- search

def _stac_search(bbox, start, end, max_cloud):
    items, url = [], f"{config.STAC_URL}/search"
    body = {"collections": [config.STAC_COLLECTION], "bbox": list(bbox),
            "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
            "query": {"eo:cloud_cover": {"lt": max_cloud}}, "limit": 100}
    while url:
        r = requests.post(url, json=body, timeout=60, headers={"User-Agent": config.USER_AGENT})
        r.raise_for_status()
        d = r.json()
        items += d["features"]
        nxt = next((l for l in d.get("links", []) if l.get("rel") == "next"), None)
        url, body = (nxt["href"], nxt.get("body", body)) if nxt else (None, None)
    return items


def mgrs_tiles_for_bbox(bbox, step=0.05):
    m = mgrs.MGRS()
    w, s, e, n = bbox
    tiles = set()
    for lat in np.arange(s, n + step, step):
        for lon in np.arange(w, e + step, step):
            tiles.add(m.toMGRS(min(lat, n), min(lon, e), MGRSPrecision=0))
    return sorted(tiles)


def _s3_list(prefix):
    keys, token = [], None
    while True:
        params = {"list-type": "2", "prefix": prefix, "delimiter": "/"}
        if token:
            params["continuation-token"] = token
        r = requests.get(config.S3_COG_BASE.rsplit("/", 1)[0] + "/", params=params, timeout=60)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = {"s": root.tag.split("}")[0].strip("{")}
        keys += [p.text for p in root.findall("s:CommonPrefixes/s:Prefix", ns)]
        if root.findtext("s:IsTruncated", namespaces=ns) != "true":
            return keys
        token = root.findtext("s:NextContinuationToken", namespaces=ns)


def _s3_search(bbox, start, end, max_cloud):
    tiles = mgrs_tiles_for_bbox(bbox)
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    months, cur = [], date(d0.year, d0.month, 1)
    while cur <= d1:
        months.append((cur.year, cur.month))
        cur = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
    prefixes = []
    for t in tiles:
        zone, band, sq = t[:2], t[2], t[3:5]
        for y, mo in months:
            prefixes.append(f"sentinel-s2-l2a-cogs/{int(zone)}/{band}/{sq}/{y}/{mo}/")
    with ThreadPoolExecutor(8) as ex:
        scene_dirs = [k for ks in ex.map(_s3_list, prefixes) for k in ks]
    keep = []
    for k in scene_dirs:
        sid = k.rstrip("/").rsplit("/", 1)[1]           # e.g. S2B_43RGM_20261005_0_L2A
        mt = re.match(r"S2[ABCD]_\w{5}_(\d{8})_\d+_L2A", sid)
        if mt and d0 <= datetime.strptime(mt.group(1), "%Y%m%d").date() <= d1:
            keep.append(k)

    def fetch(k):
        sid = k.rstrip("/").rsplit("/", 1)[1]
        r = requests.get(f"{config.S3_COG_BASE.rsplit('/', 1)[0]}/{k}{sid}.json", timeout=60)
        return r.json() if r.ok else None
    with ThreadPoolExecutor(12) as ex:
        items = [i for i in ex.map(fetch, keep) if i]
    return [i for i in items if i["properties"].get("eo:cloud_cover", 100) < max_cloud]


def search_scenes(bbox, start, end, max_cloud=config.MAX_SCENE_CLOUD):
    """Return (items, source_label). Items are STAC item dicts, de-duplicated per tile+date."""
    try:
        items, src = _stac_search(bbox, start, end, max_cloud), "Earth Search STAC API (Element84 / AWS)"
    except Exception as e:  # noqa: BLE001
        log.warning("STAC API unavailable (%s); listing the public sentinel-cogs bucket instead", e)
        items, src = _s3_search(bbox, start, end, max_cloud), "AWS sentinel-cogs bucket (direct listing)"
    best = {}
    for it in items:
        p = it["properties"]
        tile = p.get("s2:mgrs_tile") or f"{p.get('mgrs:utm_zone')}{p.get('mgrs:latitude_band')}{p.get('mgrs:grid_square')}"
        key = (tile, p["datetime"][:10])
        # Several processing runs of one acquisition can exist (_0_, _1_): keep the latest baseline.
        rank = (p.get("s2:processing_baseline", ""), it["id"])
        if key not in best or rank > best[key][0]:
            best[key] = (rank, it)
    return [v[1] for v in best.values()], src


# --------------------------------------------------------------------------- reading

def target_grid(bbox_ll, crs, res):
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    w, s, e, n = bbox_ll
    xs, ys = tr.transform([w, e, w, e], [s, s, n, n])
    minx, maxx = np.floor(min(xs) / res) * res, np.ceil(max(xs) / res) * res
    miny, maxy = np.floor(min(ys) / res) * res, np.ceil(max(ys) / res) * res
    width, height = int((maxx - minx) / res), int((maxy - miny) / res)
    return {"crs": crs, "transform": from_origin(minx, maxy, res, res), "width": width, "height": height}


# Earth Search v1 item asset names, and the v0 names still found in older bucket items.
_V0_NAMES = {"blue": "B02", "green": "B03", "red": "B04", "rededge1": "B05", "nir": "B08",
             "swir16": "B11", "swir22": "B12", "scl": "SCL"}


def _asset(item, band):
    """Return (href, scale, needs_offset_check) for one band of a STAC item.

    Processing baseline 04.00+ (Jan 2022 onwards) stores reflectance with a +1000 DN offset, but
    many COGs in the bucket already have it removed, and the item flags are not reliable: on Delhi,
    a 2022 item flagged earthsearch:boa_offset_applied=False was already corrected (blue band
    floor at 1 DN, not ~1000). So the offset is decided from the pixels themselves in _read_date.
    """
    a = item["assets"].get(band) or item["assets"][_V0_NAMES[band]]
    rb = (a.get("raster:bands") or [{}])[0]
    p = item["properties"]
    baseline = p.get("s2:processing_baseline") or p.get("sentinel:processing_baseline") or "00.00"
    return a["href"], rb.get("scale", 0.0001), baseline >= "04.00"


def _offset_from_data(dn):
    """-0.1 if the DN still carry the +1000 BOA offset, else 0. Dark surfaces (water, shadow)
    sit near 0-300 DN when the offset is removed and never below ~1000 when it is not."""
    v = dn[dn > 0]
    if v.size < 1000:
        return 0.0
    return -0.1 if np.percentile(v, 0.5) >= 950 else 0.0


def read_to_grid(href, grid, resampling, dtype="float32"):
    with rasterio.open(href) as src:
        with WarpedVRT(src, crs=grid["crs"], transform=grid["transform"], width=grid["width"],
                       height=grid["height"], resampling=resampling, src_nodata=0, nodata=0) as vrt:
            return vrt.read(1, out_dtype=dtype)


def _read_date(items, band, grid, resampling):
    """Mosaic all tiles of one acquisition date onto the grid (first non-zero wins)."""
    out = None
    for it in items:
        key = "scl" if band == "SCL" else BANDS[band]
        href, scale, check = _asset(it, key)
        a = read_to_grid(href, grid, resampling)
        if band != "SCL":
            offset = _offset_from_data(a) if check else 0.0
            valid = a > 0
            a = np.where(valid, a * scale + offset, np.nan).astype("float32")
        if out is None:
            out = a
        else:
            fill = np.isnan(out) if band != "SCL" else (out == 0)
            out[fill] = a[fill]
    return out


def build_composite(items, grid, aoi_mask, progress=lambda *_: None):
    """Pick the clearest dates for the AOI and return a per-pixel median composite.

    Returns (bands dict of float32 reflectance arrays, info dict with dates/clear-observation stats).
    """
    import warnings
    by_date = {}
    for it in items:
        by_date.setdefault(it["properties"]["datetime"][:10], []).append(it)
    # Rank dates by tile cloud cover, then measure real clearness over the city from the SCL band.
    ranked = sorted(by_date, key=lambda d: np.mean([i["properties"]["eo:cloud_cover"] for i in by_date[d]]))
    candidates = ranked[:max(12, config.MAX_COMPOSITE_DATES * 2)]
    done = [0]

    def read_scl(d):
        scl = _read_date(by_date[d], "SCL", grid, Resampling.nearest)
        done[0] += 1
        progress(f"Checking cloud cover over the city ({done[0]}/{len(candidates)} dates)",
                 0.1 + 0.2 * done[0] / len(candidates))
        return d, ~np.isin(scl, SCL_INVALID) & aoi_mask
    with ThreadPoolExecutor(6) as ex:
        valid_by_date = dict(ex.map(read_scl, candidates))

    chosen, clear_count = [], np.zeros(aoi_mask.shape, "uint8")
    aoi_px = aoi_mask.sum()
    for d in candidates:
        frac = valid_by_date[d].sum() / aoi_px
        if frac < 0.3 or len(chosen) >= config.MAX_COMPOSITE_DATES:
            continue
        chosen.append({"date": d, "clear_fraction": round(float(frac), 3),
                       "scenes": [it["id"] for it in by_date[d]]})
        clear_count += valid_by_date[d]
        if np.mean(clear_count[aoi_mask] >= config.TARGET_CLEAR_OBS) > 0.97:
            break
    if not chosen:
        raise RuntimeError("No sufficiently clear Sentinel-2 acquisitions in this period. Try a longer window.")
    chosen.sort(key=lambda c: c["date"])

    bands, n_jobs, done[0] = {}, len(BANDS) * len(chosen), 0

    def read_band(args):
        b, d = args
        a = _read_date(by_date[d], b, grid, Resampling.average)
        a[~valid_by_date[d]] = np.nan
        done[0] += 1
        progress(f"Reading Sentinel-2 bands ({done[0]}/{n_jobs})", 0.3 + 0.55 * done[0] / n_jobs)
        return a
    with ThreadPoolExecutor(6) as ex:
        for b in BANDS:   # one band at a time keeps memory to (dates x one band)
            stack = np.stack(list(ex.map(read_band, [(b, c["date"]) for c in chosen])))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                bands[b] = np.nanmedian(stack, axis=0).astype("float32")
            del stack
    info = {"dates": chosen,
            "candidate_dates": len(by_date),
            "latest_acquisition": max(by_date),
            "clear_obs_mean": round(float(clear_count[aoi_mask].mean()), 2),
            "aoi_coverage": round(float(np.mean(clear_count[aoi_mask] > 0)), 4)}
    return bands, info


def default_period(end=None, days=config.DEFAULT_WINDOW_DAYS):
    end = date.fromisoformat(end) if end else date.today()
    return (end - timedelta(days=days)).isoformat(), end.isoformat()
