"""FastAPI backend: serves the dashboard, runs date-wise analyses as background jobs, exposes results."""
import json
import logging
import threading
import traceback
import uuid
from datetime import date, timedelta
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sentinel_city import analysis, config, geocode, series

log = logging.getLogger(__name__)
app = FastAPI(title="City Land Monitor")
WEB = config.ROOT / "web"
config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=WEB), name="static")
app.mount("/results", StaticFiles(directory=config.RESULTS_DIR), name="results")

JOBS = {}
_lock = threading.Lock()


class AnalyzeRequest(BaseModel):
    city: str
    dates: Optional[List[date]] = None      # one composite per date; default: today and a year ago
    window_days: int = 30                   # imagery window before each date
    resolution: int = config.DEFAULT_RESOLUTION


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/runs")
def runs():
    p = config.RESULTS_DIR / "index.json"
    return json.loads(p.read_text()) if p.exists() else series.rebuild_index()


@app.get("/api/runs/{slug}/{run_id}")
def run_summary(slug: str, run_id: str):
    p = config.RESULTS_DIR / slug / run_id / "summary.json"
    if not p.exists():
        raise HTTPException(404, "run not found")
    return json.loads(p.read_text())


@app.get("/api/series/{slug}/{sid}")
def get_series(slug: str, sid: str):
    p = config.RESULTS_DIR / slug / "series" / f"{sid}.json"
    if not p.exists():
        raise HTTPException(404, "series not found")
    return json.loads(p.read_text())


@app.get("/api/changes/{slug}/{cid}")
def get_change(slug: str, cid: str):
    p = config.RESULTS_DIR / slug / "changes" / cid / "change.json"
    if not p.exists():
        raise HTTPException(404, "change not found")
    return json.loads(p.read_text())


@app.get("/api/places")
def places(q: str):
    """City-box autocomplete: area names only (cities, districts, localities), never buildings or roads."""
    return geocode.suggest_places(q.strip())[:8] if q.strip() else []


@app.get("/api/localities/{slug}")
def localities(slug: str):
    p = config.BOUNDARY_DIR / slug / "localities.json"
    return json.loads(p.read_text()) if p.exists() else []


@app.get("/api/point")
def point(slug: str, run_id: str, lat: float, lon: float):
    try:
        return analysis.point_query(slug, run_id, lat, lon)
    except FileNotFoundError:
        raise HTTPException(404, "run not found")


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    if req.resolution not in (10, 20, 30, 60):
        raise HTTPException(400, "resolution must be 10, 20, 30 or 60")
    if not 5 <= req.window_days <= 120:
        raise HTTPException(400, "window_days must be between 5 and 120")
    today = date.today()
    dates = req.dates or [today - timedelta(days=365), today]
    if any(d > today for d in dates):
        raise HTTPException(400, "dates cannot be in the future")
    if any(d < date(2017, 1, 1) for d in dates):
        raise HTTPException(400, "Sentinel-2 surface-reflectance imagery in this archive starts in 2017")
    if len(set(dates)) > 12:
        raise HTTPException(400, "pick at most 12 dates")
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "status": "running", "message": "Queued", "progress": 0.0, "city": req.city,
           "result": None, "error": None}

    def work():
        def prog(msg, frac):
            with _lock:
                job.update(message=msg, progress=round(frac, 3))
        try:
            s = series.analyze_dates(req.city, [d.isoformat() for d in dates], req.window_days, req.resolution, prog)
            job.update(status="done", progress=1.0, message="Done", result={"slug": s["slug"], "series_id": s["id"]})
        except Exception as e:  # noqa: BLE001
            log.error(traceback.format_exc())
            job.update(status="error", error=str(e), message="Failed")
    JOBS[job_id] = job
    threading.Thread(target=work, daemon=True).start()
    return job


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "unknown job")
    return JOBS[job_id]
