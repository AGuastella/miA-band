"""Scenario-driven synthetic data in the canonical schema, with ground truth (docs/SPEC.md §10).

Each night is generated in local wall-clock time (zoneinfo, so DST is real), then converted to
UTC exactly as a device would record it, including the per-record offset. Planted events have
known expected outputs in the returned `truth` frame, so feature tests assert exact values.

Night model (wake date D): bedtime on D-1 at `bedtime` (+ shift), lasting `sleep_hours`.
Stages cycle light 45 / deep 25 / REM 20 min; an optional awake bout sits mid-night.
Sleeping HR: a V-shape descending to `nadir_hr` (+ illness delta) with a flat 40-min plateau
in the middle of the night, so the true RHR (lowest 10-min rolling mean) is exactly the nadir
when noise_sd = 0.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..canonical import TABLES, CanonicalBatch

SOURCE = "synthetic"


@dataclass
class Scenario:
    first_wake: dt.date = dt.date(2026, 3, 1)
    nights: int = 42
    tz: str = "Europe/Madrid"
    device_id: str = "synthetic-band"
    seed: int = 0
    hr_cadence_s: int = 60
    noise_sd: float = 0.0
    day_hr: float = 72.0
    nadir_hr: float = 52.0
    hr_max: float = 191.0                                   # the band's zone thresholds use this
    bedtime: dt.time = dt.time(23, 30)
    sleep_hours: float = 7.5
    bedtime_jitter_min: float = 0.0
    with_stages: bool = True
    # planted events, keyed by wake date
    short_nights: dict = field(default_factory=dict)        # date -> hours
    bedtime_shift_min: dict = field(default_factory=dict)   # date -> minutes (+ later)
    awake_bouts: dict = field(default_factory=dict)         # date -> minutes awake mid-night
    rhr_delta: dict = field(default_factory=dict)           # date -> bpm added to the nadir
    missing_nights: set = field(default_factory=set)        # no sleep record, band off at night
    naps: dict = field(default_factory=dict)                # date -> (local time, minutes)
    workouts: dict = field(default_factory=dict)            # date -> (local time, minutes, bpm)
    non_wear: list = field(default_factory=list)            # [(local datetime, local datetime)]
    tz_by_date: dict = field(default_factory=dict)          # date -> IANA zone (travel)


def _utc(local: dt.datetime, tz: ZoneInfo) -> int:
    return int(local.replace(tzinfo=tz).timestamp())


def _offset_min(ts: int, tz: ZoneInfo) -> int:
    return int(dt.datetime.fromtimestamp(ts, tz).utcoffset().total_seconds() // 60)


def _stage_plan(start: int, end: int, awake_min: float) -> list[tuple[int, int, str]]:
    cycle = [("light", 45), ("deep", 25), ("rem", 20)]
    segs, t, k = [], start, 0
    while t < end:
        stage, m = cycle[k % 3]
        segs.append((t, min(t + m * 60, end), stage))
        t += m * 60
        k += 1
    if awake_min:
        mid = start + (end - start) // 2
        a0, a1 = mid, mid + int(awake_min * 60)
        cut = []
        for s, e, st in segs:      # carve the awake bout out of whatever it overlaps
            if e <= a0 or s >= a1:
                cut.append((s, e, st))
                continue
            if s < a0:
                cut.append((s, a0, st))
            if e > a1:
                cut.append((a1, e, st))
        segs = sorted(cut + [(a0, a1, "awake")])
    return segs


def generate(sc: Scenario) -> tuple[CanonicalBatch, pd.DataFrame]:
    rng = np.random.default_rng(sc.seed)
    home = ZoneInfo(sc.tz)
    sessions, segments, workouts, truth = [], [], [], []
    sleep_spans, nadir_by_span = [], []

    for i in range(sc.nights):
        wake = sc.first_wake + dt.timedelta(days=i)
        tz = ZoneInfo(sc.tz_by_date.get(wake, sc.tz))
        hours = sc.short_nights.get(wake, sc.sleep_hours)
        shift = sc.bedtime_shift_min.get(wake, 0) + rng.normal(0, sc.bedtime_jitter_min)
        bed_local = dt.datetime.combine(wake - dt.timedelta(days=1), sc.bedtime) + dt.timedelta(minutes=shift)
        start = _utc(bed_local, tz)
        end = start + int(hours * 3600)
        awake = sc.awake_bouts.get(wake, 0)
        nadir = sc.nadir_hr + sc.rhr_delta.get(wake, 0)
        missing = wake in sc.missing_nights
        truth.append(dict(date=np.datetime64(wake), has_night=not missing,
                          tst_min=np.nan if missing else hours * 60 - awake,
                          spt_min=np.nan if missing else hours * 60,
                          onset_clock=(bed_local.hour * 60 + bed_local.minute) if not missing else np.nan,
                          rhr=np.nan if missing else nadir))
        if missing:
            sleep_spans.append((start, end, None))
            continue
        off = _offset_min(start, tz)
        sessions.append(dict(start_ts=start, end_ts=end, is_nap=np.nan, tz_offset_min=off,
                             dev_duration_min=hours * 60 - awake, dev_awake_min=float(awake)))
        if sc.with_stages:
            for s, e, st in _stage_plan(start, end, awake):
                segments.append(dict(session_start_ts=start, start_ts=s, end_ts=e, stage=st))
        sleep_spans.append((start, end, nadir))

        if wake in sc.naps:
            t, minutes = sc.naps[wake]
            ns = _utc(dt.datetime.combine(wake, t), tz)
            sessions.append(dict(start_ts=ns, end_ts=ns + minutes * 60, is_nap=np.nan,
                                 tz_offset_min=_offset_min(ns, tz), dev_duration_min=float(minutes),
                                 dev_awake_min=0.0))
            if sc.with_stages:
                segments.append(dict(session_start_ts=ns, start_ts=ns, end_ts=ns + minutes * 60, stage="light"))
        if wake in sc.workouts:
            t, minutes, bpm = sc.workouts[wake]
            ws = _utc(dt.datetime.combine(wake, t), tz)
            zone = int(np.searchsorted((0.5, 0.6, 0.7, 0.8, 0.9), bpm / sc.hr_max, side="right"))
            workouts.append(dict(start_ts=ws, end_ts=ws + minutes * 60, sport="beach_volleyball",
                                 tz_offset_min=_offset_min(ws, tz), avg_hr=float(bpm), max_hr=float(bpm),
                                 min_hr=float(bpm), dev_train_load=np.nan,
                                 **{f"zone{z}_s": float(minutes * 60 if z == zone else 0) for z in range(1, 6)}))

    # ---- HR stream over the whole span
    t0 = _utc(dt.datetime.combine(sc.first_wake - dt.timedelta(days=1), dt.time(12)), home)
    t1 = _utc(dt.datetime.combine(sc.first_wake + dt.timedelta(days=sc.nights - 1), dt.time(12)), home)
    ts = np.arange(t0, t1, sc.hr_cadence_s, dtype="int64")
    hr = np.full(ts.shape, sc.day_hr, dtype=float)
    keep = np.ones(ts.shape, dtype=bool)
    for start, end, nadir in sleep_spans:
        m = (ts >= start) & (ts < end)
        if nadir is None:            # missing night: band off overnight
            keep &= ~m
            continue
        x = (ts[m] - start) / (end - start)                     # 0..1 through the night
        dist = np.clip(np.abs(x - 0.5) - (20 * 60) / (end - start), 0, None)   # 40-min plateau
        hr[m] = nadir + 30 * dist
    for w in workouts:
        hr[(ts >= w["start_ts"]) & (ts < w["end_ts"])] = w["avg_hr"]
    for a, b in sc.non_wear:
        keep &= ~((ts >= _utc(a, home)) & (ts < _utc(b, home)))
    hr = hr + rng.normal(0, sc.noise_sd, ts.shape) if sc.noise_sd else hr

    def frame(rows, table):
        df = pd.DataFrame(rows, columns=[c for c in TABLES[table]["columns"] if c not in ("source", "device_id")])
        df.insert(0, "device_id", sc.device_id)
        df.insert(0, "source", SOURCE)
        return df[list(TABLES[table]["columns"])]

    batch = CanonicalBatch(source=SOURCE, tables={
        "hr_samples": frame(pd.DataFrame({"ts": ts[keep], "bpm": np.round(hr[keep]).astype(int),
                                          "context": "background"}), "hr_samples"),
        "sleep_sessions": frame(pd.DataFrame(sessions), "sleep_sessions"),
        "sleep_segments": frame(pd.DataFrame(segments), "sleep_segments"),
        "workouts": frame(pd.DataFrame(workouts), "workouts"),
    })
    batch.validate()
    return batch, pd.DataFrame(truth)
