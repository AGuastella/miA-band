"""Read-only dashboard over the daily_metrics store (docs/SPEC.md §1, Step 5).

The API serves what `miaband compute` wrote; it never recomputes. Every value travels with its
status and reason, so the page can show *why* something is missing instead of a blank.

  GET /api/range                 first/last date, default selected date
  GET /api/day/{date}            all metrics for one day: {metric: {value, status, reason}}
  GET /api/series?end=&days=     wide arrays for charts over a window ending at `end`
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).parent / "static"

SERIES = [
    "recovery.score", "phys.rhr", "rhr.base28", "rhr.spread28", "rhr.flag",
    "sleep.tst_min", "tst.base28", "tst.flag", "sleep.sri_7d",
    "strain.load", "strain.strain", "acwr.ewma",
]


def create_app(store_path: Path | str) -> FastAPI:
    store_path = Path(store_path)
    app = FastAPI(title="miA-band", docs_url=None, redoc_url=None)

    def con() -> sqlite3.Connection:
        if not store_path.exists():
            raise HTTPException(503, f"no store at {store_path}: run `python -m miaband compute`")
        c = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c

    @app.get("/api/range")
    def range_():
        with con() as c:
            lo, hi = c.execute("SELECT MIN(date), MAX(date) FROM daily_metrics").fetchone()
            last_ok = c.execute("SELECT MAX(date) FROM daily_metrics WHERE metric = 'recovery.score' "
                                "AND status = 'ok'").fetchone()[0]
        if lo is None:
            raise HTTPException(404, "daily_metrics is empty: run `python -m miaband compute`")
        return {"min": lo, "max": hi, "default": last_ok or hi}

    @app.get("/api/day/{date}")
    def day(date: str):
        _check_date(date)
        with con() as c:
            rows = c.execute("SELECT metric, value, status, reason FROM daily_metrics WHERE date = ?",
                             (date,)).fetchall()
        if not rows:
            raise HTTPException(404, f"no metrics for {date}")
        return {"date": date, "metrics": {r["metric"]: {"value": r["value"], "status": r["status"],
                                                       "reason": r["reason"]} for r in rows}}

    @app.get("/api/series")
    def series(end: str, days: int = Query(28, ge=7, le=3660)):
        end_d = _check_date(end)
        start = (end_d - dt.timedelta(days=days - 1)).isoformat()
        dates = [(end_d - dt.timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
        idx = {d: i for i, d in enumerate(dates)}
        out = {m: {"value": [None] * days, "status": [None] * days, "reason": [None] * days} for m in SERIES}
        marks = ",".join("?" * len(SERIES))
        with con() as c:
            for r in c.execute(f"SELECT date, metric, value, status, reason FROM daily_metrics "
                               f"WHERE date BETWEEN ? AND ? AND metric IN ({marks})", (start, end, *SERIES)):
                i = idx[r["date"]]
                s = out[r["metric"]]
                s["value"][i], s["status"][i], s["reason"][i] = r["value"], r["status"], r["reason"]
        return {"dates": dates, "series": out}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _check_date(s: str) -> dt.date:
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"bad date {s!r}, expected YYYY-MM-DD") from None
