"""Read-only dashboard over the daily_metrics store (docs/SPEC.md §1, Step 5).

The API serves what `miaband compute` wrote; it never recomputes. Every value travels with its
status and reason, so the page can show *why* something is missing instead of a blank.

  GET /api/range                 first/last date, default selected date
  GET /api/day/{date}            all metrics for one day: {metric: {value, status, reason}}
  GET /api/series?end=&days=     wide arrays for charts over a window ending at `end`
  GET /api/period?end=&days=     training + steps summary of that window vs the previous one
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
    "strain.load", "strain.strain", "acwr.ewma", "activity.steps",
]
ZONES = ["z1_min", "z2_min", "z3_min", "z4_min", "z5_min"]


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

    @app.get("/api/period")
    def period(end: str, days: int = Query(30, ge=1, le=3660)):
        end_d = _check_date(end)
        start_d = end_d - dt.timedelta(days=days - 1)
        prev_end = start_d - dt.timedelta(days=1)
        prev_start = prev_end - dt.timedelta(days=days - 1)
        with con() as c:
            cur = _summary(c, start_d, end_d)
            prev = _summary(c, prev_start, prev_end)
            sessions = [dict(r) for r in c.execute(
                "SELECT date, start_ts, sport, method, duration_min, avg_hr, peak_hr, load, "
                + ", ".join(ZONES) + " FROM workout_metrics WHERE date BETWEEN ? AND ? "
                "ORDER BY start_ts DESC LIMIT 500", (start_d.isoformat(), end_d.isoformat()))]
        return {"start": start_d.isoformat(), "end": end_d.isoformat(), "days": days,
                "current": cur, "previous": prev, "sessions": sessions}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _summary(c: sqlite3.Connection, start: dt.date, end: dt.date) -> dict:
    """Totals for [start, end]. Known/unknown days are counted so nothing is silently averaged in."""
    a, b = start.isoformat(), end.isoformat()
    has_table = c.execute("SELECT 1 FROM sqlite_master WHERE name = 'workout_metrics'").fetchone()
    w = c.execute(
        "SELECT COUNT(*) n, SUM(method != 'detected') rec, SUM(method = 'detected') det, "
        "SUM(load IS NULL) unknown, SUM(duration_min) dur, SUM(load) load, "
        "SUM(avg_hr * duration_min) / NULLIF(SUM(CASE WHEN avg_hr IS NOT NULL THEN duration_min END), 0) avg_hr, "
        "MAX(peak_hr) peak, " + ", ".join(f"SUM({z}) {z}" for z in ZONES) +
        " FROM workout_metrics WHERE date BETWEEN ? AND ?", (a, b)).fetchone() if has_table else None
    by_sport = [dict(r) for r in c.execute(
        "SELECT sport, COUNT(*) n, SUM(duration_min) / 60.0 hours, SUM(load) load "
        "FROM workout_metrics WHERE date BETWEEN ? AND ? GROUP BY sport ORDER BY hours DESC", (a, b))] if has_table else []
    def daily(metric):
        r = c.execute("SELECT COUNT(*) known, SUM(value) total, MAX(value) mx FROM daily_metrics "
                      "WHERE metric = ? AND status = 'ok' AND date BETWEEN ? AND ?", (metric, a, b)).fetchone()
        top = c.execute("SELECT date FROM daily_metrics WHERE metric = ? AND status = 'ok' AND date BETWEEN ? AND ? "
                        "ORDER BY value DESC LIMIT 1", (metric, a, b)).fetchone()
        return {"known_days": r["known"], "total": r["total"],
                "per_day": (r["total"] / r["known"]) if r["known"] else None,
                "max": r["mx"], "max_date": top["date"] if top else None}
    steps, load = daily("activity.steps"), daily("strain.load")
    n_days = (end - start).days + 1
    return {
        "sessions": (w["n"] or 0) if w else 0, "recorded": (w["rec"] or 0) if w else 0,
        "detected": (w["det"] or 0) if w else 0, "unknown_load": (w["unknown"] or 0) if w else 0,
        "hours": (w["dur"] or 0) / 60 if w else 0, "load": load["total"], "load_known_days": load["known_days"],
        "load_per_week": (load["total"] / load["known_days"] * 7) if load["known_days"] else None,
        "avg_hr": w["avg_hr"] if w else None, "peak_hr": w["peak"] if w else None,
        "zones_min": [w[z] or 0 for z in ZONES] if w else [0] * 5,
        "by_sport": by_sport, "steps": steps, "days": n_days,
    }


def _check_date(s: str) -> dt.date:
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"bad date {s!r}, expected YYYY-MM-DD") from None
