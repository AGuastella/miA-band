"""Per-night sleep metrics and rolling regularity (docs/SPEC.md §4.2–4.3).

TST  = minutes of non-awake segments inside the main episode (or, without segments, session
       duration minus device-reported awake time).
SPT  = onset -> final awakening; WASO = SPT - TST (includes gaps between merged sessions).
SME  = TST / SPT  ("sleep maintenance efficiency"; the band's time-in-bed is not a measured bed
       entry, so true sleep efficiency is not reported - SPEC §9.2).
Stages are reported only for nights where the device produced REM (older bands could not).

Regularity over a rolling window ending at each wake date:
  SRI (Phillips et al. 2017) on a per-minute local-clock sleep/wake grid, comparing each day
  with the previous one; minutes with unknown state (not worn, not asleep) are excluded, and a
  day pair counts only if >= `sri_min_pair_coverage` of its minutes are known on both days.
  Circular SD of onset and wake clock times (minutes); 23:50 vs 00:10 is 20 min apart.
Nights within `tz_change_exclude_nights` after a time-zone change are excluded from both.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from ..config import Config
from .timeutil import DAY, local_clock_min, local_offsets


def nightly_sleep(episodes: pd.DataFrame, ep_segments: pd.DataFrame, sessions: pd.DataFrame) -> pd.DataFrame:
    """One row per wake date with a main episode."""
    main = episodes[episodes["role"] == "main"]
    rows = []
    for ep in main.itertuples(index=False):
        seg = ep_segments[ep_segments["episode"] == ep.episode]
        spt = (ep.end_ts - ep.start_ts) / 60
        stages = dict.fromkeys(("awake", "light", "deep", "rem", "asleep"), 0.0)
        if not seg.empty:
            s = np.clip(seg["start_ts"].to_numpy(), ep.start_ts, ep.end_ts)
            e = np.clip(seg["end_ts"].to_numpy(), ep.start_ts, ep.end_ts)
            mins = (e - s) / 60
            for st, m in zip(seg["stage"], mins):
                stages[st] += m
            tst = sum(v for k, v in stages.items() if k != "awake")
            tst_basis = "segments"
        else:
            members = sessions.set_index(["source", "device_id", "start_ts"]).loc[ep.members]
            tst = float(((members["end_ts"] - members["start_ts"]) / 60
                         - members["dev_awake_min"].fillna(0)).sum())
            tst_basis = "envelope"
        has_stages = stages["rem"] > 0
        rows.append(dict(
            date=ep.wake_date, episode=ep.episode, source=ep.source, device_id=ep.device_id,
            onset_ts=ep.start_ts, wake_ts=ep.end_ts, offset_start=ep.offset_start,
            offset_end=ep.offset_end,
            onset_clock=int(local_clock_min(ep.start_ts, ep.offset_start)),
            wake_clock=int(local_clock_min(ep.end_ts, ep.offset_end)),
            spt_min=spt, tst_min=tst, waso_min=max(spt - tst, 0.0),
            sme=tst / spt if spt > 0 else np.nan, tst_basis=tst_basis,
            deep_min=stages["deep"] if has_stages else np.nan,
            light_min=stages["light"] if has_stages else np.nan,
            rem_min=stages["rem"] if has_stages else np.nan,
            stages_available=has_stages))
    nights = pd.DataFrame(rows)
    if nights.empty:
        return nights
    naps = (episodes[episodes["role"] == "nap"]
            .assign(m=lambda d: (d["end_ts"] - d["start_ts"]) / 60)
            .groupby("wake_date")["m"].sum())
    nights["nap_min"] = nights["date"].map(naps).fillna(0.0)
    prev = nights["offset_end"].shift(1)
    diff = (nights["offset_end"] - prev).abs()
    dst = (diff == 60) & nights["date"].map(_near_eu_dst_switch)
    nights["tz_change"] = prev.notna() & (diff > 0) & ~dst
    return nights.sort_values("date").reset_index(drop=True)


def _near_eu_dst_switch(day, tolerance_days: int = 1) -> bool:
    """Within a day of an EU summer-time switch (last Sunday of March / October)."""
    d = pd.Timestamp(day).date()
    for month in (3, 10):
        last = dt.date(d.year, month, 31)
        switch = last - dt.timedelta(days=(last.weekday() + 1) % 7)
        if abs((d - switch).days) <= tolerance_days:
            return True
    return False


def _excluded_after_tz_change(nights: pd.DataFrame, n_after: int) -> pd.Series:
    """True for nights within n_after nights (inclusive of the change night) after a tz change."""
    excl = pd.Series(False, index=nights.index)
    for i in np.flatnonzero(nights["tz_change"].to_numpy()):
        excl.iloc[i:i + n_after] = True
    return excl


def circular_sd_min(clock_min: np.ndarray) -> float:
    theta = np.asarray(clock_min, dtype=float) / 1440 * 2 * np.pi
    r = np.hypot(np.cos(theta).mean(), np.sin(theta).mean())
    return float(np.sqrt(-2 * np.log(min(max(r, 1e-12), 1.0))) * 1440 / (2 * np.pi))


def _grid_mark(grid: np.ndarray, base_min: int, a_local_s: np.ndarray, b_local_s: np.ndarray, value: float) -> None:
    a = (np.asarray(a_local_s) // 60 - base_min).astype("int64")
    b = (-(-np.asarray(b_local_s) // 60) - base_min).astype("int64")   # ceil
    for i, j in zip(a.clip(0, grid.size), b.clip(0, grid.size)):
        if j > i:
            grid[i:j] = value


def sleep_wake_grid(episodes: pd.DataFrame, ep_segments: pd.DataFrame, worn: pd.DataFrame,
                    timeline: pd.DataFrame, cfg: Config) -> tuple[np.ndarray, np.datetime64]:
    """Per-minute local-clock state: 1 asleep, 0 awake (worn, not asleep), NaN unknown.

    Interval endpoints are converted with the offset in force at each endpoint, so a night that
    spans a DST change lands on the right wall-clock minutes.
    """
    if episodes.empty:
        return np.zeros((0, 1440)), np.datetime64("1970-01-01")
    def loc(ts):
        ts = np.asarray(ts, dtype="int64")
        return ts + local_offsets(ts, timeline, cfg.tz) * 60
    first_day = (loc(episodes["start_ts"]).min() // DAY) - 1
    last_day = (loc(episodes["end_ts"]).max() // DAY) + 1
    if not worn.empty:
        first_day = min(first_day, loc(worn["start_ts"]).min() // DAY)
        last_day = max(last_day, loc(worn["end_ts"]).max() // DAY)
    n_days = int(last_day - first_day + 1)
    grid = np.full(n_days * 1440, np.nan)
    base_min = int(first_day * 1440)
    if not worn.empty:
        _grid_mark(grid, base_min, loc(worn["start_ts"]), loc(worn["end_ts"]), 0.0)
    _grid_mark(grid, base_min, loc(episodes["start_ts"]), loc(episodes["end_ts"]), 1.0)
    awake = ep_segments[ep_segments["stage"] == "awake"] if not ep_segments.empty else ep_segments
    if not awake.empty:
        _grid_mark(grid, base_min, loc(awake["start_ts"]), loc(awake["end_ts"]), 0.0)
    return grid.reshape(n_days, 1440), np.datetime64(int(first_day), "D")


def regularity(nights: pd.DataFrame, grid: np.ndarray, grid_day0: np.datetime64, cfg: Config) -> pd.DataFrame:
    """Rolling SRI and circular SDs, one row per wake date in `nights`."""
    win, min_n = cfg.sleep.regularity_window_days, cfg.sleep.regularity_min_nights
    out = []
    if nights.empty:
        return pd.DataFrame(columns=["date", "sri", "onset_csd", "wake_csd", "reg_n", "reg_status",
                                     "sri_pairs", "sri_reason"])
    # pandas stores dates as datetime64[s]; day arithmetic below needs [D] numpy values.
    all_days = nights["date"].to_numpy().astype("datetime64[D]")
    excl = _excluded_after_tz_change(nights, cfg.sleep.tz_change_exclude_nights).to_numpy()
    usable = nights[~excl]
    dates = all_days[~excl]
    excluded_days = set(all_days[excl].tolist())
    for d in all_days:
        lo = d - np.timedelta64(win - 1, "D")
        sel = usable[(dates >= lo) & (dates <= d)]
        n = len(sel)
        row = dict(date=d, reg_n=n, sri=np.nan, onset_csd=np.nan, wake_csd=np.nan, sri_pairs=0,
                   sri_reason=f"only {n} usable nights in the {win}-day window (need {min_n})")
        if n < min_n:
            row["reg_status"] = "insufficient_data"
            out.append(row)
            continue
        row["onset_csd"] = circular_sd_min(sel["onset_clock"])
        row["wake_csd"] = circular_sd_min(sel["wake_clock"])
        agree = known = pairs = 0
        for k in range(win):
            day = d - np.timedelta64(k, "D")
            i = int((day - grid_day0).astype(int))
            if i < 1 or i >= len(grid) or day.tolist() in excluded_days or (day - np.timedelta64(1, "D")).tolist() in excluded_days:
                continue
            a, b = grid[i], grid[i - 1]
            ok = ~np.isnan(a) & ~np.isnan(b)
            if ok.sum() < cfg.sleep.sri_min_pair_coverage * 1440:
                continue
            pairs += 1
            known += ok.sum()
            agree += (a[ok] == b[ok]).sum()
        row["sri_pairs"] = pairs
        if pairs >= min_n:
            row["sri"] = 200.0 * agree / known - 100.0
            row["sri_reason"] = None
        else:
            row["sri_reason"] = (f"only {pairs} day pairs with >= {cfg.sleep.sri_min_pair_coverage:.0%} of minutes "
                                 f"known on both days (need {min_n}): band off for long stretches")
        row["reg_status"] = "ok"
        out.append(row)
    return pd.DataFrame(out)
