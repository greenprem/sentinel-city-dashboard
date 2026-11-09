"""Date-wise analysis: one composite per chosen date, then change between consecutive dates.

Each date gets a composite of the clearest imagery in the `window_days` before it, so the
numbers describe the city "as of" that date. If a window has too little clear imagery
(monsoon cloud, for example) it is widened automatically, and the series records that.
"""
import hashlib
import json
import logging
from datetime import date, timedelta

from . import analysis, change, config, geocode

log = logging.getLogger(__name__)

MAX_WINDOW_DAYS = 120
MIN_COVERAGE = 0.9


def _analyze_date(city, d, window, res, progress):
    w = window
    while True:
        start = (date.fromisoformat(d) - timedelta(days=w)).isoformat()
        try:
            s = analysis.analyze_city(city, start, d, res, progress=progress)
            if s["imagery"]["aoi_coverage"] >= MIN_COVERAGE or w >= MAX_WINDOW_DAYS:
                return s, w
            log.info("Only %.0f%% clear coverage for %s with %d days; widening", 100 * s["imagery"]["aoi_coverage"], d, w)
        except RuntimeError as e:
            if w >= MAX_WINDOW_DAYS:
                raise RuntimeError(f"{d}: {e}") from e
            log.info("%s with %d-day window: %s; widening", d, w, e)
        w = min(MAX_WINDOW_DAYS, w * 2)


def series_id(dates, window, res):
    key = "_".join(dates)
    if len(key) > 60:
        key = f"{dates[0]}_to_{dates[-1]}_{len(dates)}dates_" + hashlib.sha1(key.encode()).hexdigest()[:6]
    return f"{key}_{window}d_{res}m"


def analyze_dates(city_name, dates, window_days=30, res=config.DEFAULT_RESOLUTION, progress=None):
    dates = sorted(set(dates))
    if len(dates) < 1:
        raise ValueError("Pick at least one date")
    slug = geocode.geocode_city(city_name)["slug"]
    n = len(dates)
    entries = []
    for i, d in enumerate(dates):
        def prog(msg, frac, i=i, d=d):
            if progress:
                progress(f"Date {i + 1} of {n} ({d}): {msg}", 0.9 * (i + frac) / n)
        s, used = _analyze_date(city_name, d, window_days, res, prog)
        entries.append({"date": d, "run_id": analysis.run_dir(slug, s["period"]["start"], d, res).name,
                        "start": s["period"]["start"], "end": d, "window_days": used,
                        "imagery_dates": [x["date"] for x in s["imagery"]["dates"]]})
    changes = []
    pairs = [(i, i + 1) for i in range(n - 1)]
    if n > 2:
        pairs.append((0, n - 1))
    for k, (i, j) in enumerate(pairs):
        if progress:
            progress(f"Computing change {dates[i]} to {dates[j]}", 0.9 + 0.1 * k / len(pairs))
        change.compute_change(slug, entries[i]["run_id"], entries[j]["run_id"])
        changes.append({"from": dates[i], "to": dates[j], "id": f"{entries[i]['run_id']}__{entries[j]['run_id']}"})
    sid = series_id(dates, window_days, res)
    series = {"id": sid, "slug": slug, "city": city_name, "dates": entries, "window_days": window_days,
              "resolution_m": res, "changes": changes}
    sdir = config.RESULTS_DIR / slug / "series"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / f"{sid}.json").write_text(json.dumps(series, indent=2))
    rebuild_index()
    if progress:
        progress("Done", 1.0)
    return series


def rebuild_index():
    """results/index.json lists every city, run and date series found on disk."""
    idx = {"cities": {}}
    for cdir in sorted(p for p in config.RESULTS_DIR.iterdir() if p.is_dir()):
        runs = []
        name = cdir.name
        for rdir in sorted(cdir.iterdir()):
            sp = rdir / "summary.json"
            if not sp.exists():
                continue
            s = json.loads(sp.read_text())
            name = s["city"]["name"]
            runs.append({"id": rdir.name, "start": s["period"]["start"], "end": s["period"]["end"],
                         "resolution_m": s["resolution_m"], "generated_at": s["generated_at"]})
        series = []
        for sp in sorted((cdir / "series").glob("*.json")) if (cdir / "series").exists() else []:
            s = json.loads(sp.read_text())
            series.append({"id": s["id"], "dates": [d["date"] for d in s["dates"]], "window_days": s["window_days"],
                           "resolution_m": s["resolution_m"], "modified": sp.stat().st_mtime})
        if runs:
            idx["cities"][cdir.name] = {"name": name, "runs": runs, "series": series}
    (config.RESULTS_DIR / "index.json").write_text(json.dumps(idx, indent=2))
    return idx
