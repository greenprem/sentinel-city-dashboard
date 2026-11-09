"""Command-line entry point.

    python run.py analyze "Delhi, India"                       # last 60 days
    python run.py analyze "Delhi, India" --start 2025-10-01 --end 2025-11-30 --res 10
    python run.py compare "Delhi, India" 2018-10-11 2022-10-11 2026-10-11   # date-wise + change
    python run.py serve                                        # dashboard on http://localhost:8000
"""
import argparse
import json
import logging


def main():
    ap = argparse.ArgumentParser(description="Sentinel-2 city land-cover dashboard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="run an analysis and store it under results/")
    a.add_argument("city")
    a.add_argument("--start")
    a.add_argument("--end")
    a.add_argument("--res", type=int, default=None, help="pixel size in metres (10 or 20)")
    a.add_argument("--force", action="store_true", help="recompute even if a cached result exists")
    c = sub.add_parser("compare", help="analyse several dates and the change between them")
    c.add_argument("city")
    c.add_argument("dates", nargs="+", help="YYYY-MM-DD dates (2017 onwards)")
    c.add_argument("--window", type=int, default=30, help="days of imagery before each date")
    c.add_argument("--res", type=int, default=None)
    s = sub.add_parser("serve", help="start the web dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("rasterio").setLevel(logging.WARNING)

    if args.cmd == "analyze":
        from sentinel_city import analysis, config
        s = analysis.analyze_city(args.city, args.start, args.end, args.res or config.DEFAULT_RESOLUTION,
                                  force=args.force)
        print(json.dumps({k: s[k] for k in ("city", "period", "metrics", "water_quality", "narrative")}, indent=2))
    elif args.cmd == "compare":
        from sentinel_city import config, series
        s = series.analyze_dates(args.city, args.dates, args.window, args.res or config.DEFAULT_RESOLUTION)
        print(json.dumps(s, indent=2))
    else:
        import uvicorn
        uvicorn.run("server.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
