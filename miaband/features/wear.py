"""Wear inference from HR continuity (docs/SPEC.md §3).

Consecutive valid HR samples at most `gap_max = max(3 * cadence, floor)` apart are joined into a
worn interval; a longer gap is non-wear (band off, charging, or no data). Cadence is estimated
per sample as the median spacing of the surrounding samples, so a device switch from 10-min to
1-min sampling is handled without configuration.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

HR_VALID = (25, 230)


def valid_hr(hr: pd.DataFrame) -> pd.DataFrame:
    """Background/workout samples in the physiological range, sorted, one per timestamp."""
    if hr.empty:
        return hr.assign(ts=hr["ts"].astype("int64"))
    d = hr[hr["context"].isin(["background", "workout"]) & hr["bpm"].between(*HR_VALID)]
    d = d.sort_values("ts", kind="stable").drop_duplicates("ts")
    return d.assign(ts=d["ts"].astype("int64"), bpm=d["bpm"].astype(float)).reset_index(drop=True)


def local_cadence(ts: np.ndarray, window: int = 31) -> np.ndarray:
    """Per-sample cadence (s): rolling median of gaps, ignoring gaps > 1 h (non-wear)."""
    if len(ts) < 2:
        return np.full(len(ts), 60.0)
    gaps = np.diff(ts).astype(float)
    gaps[gaps > 3600] = np.nan
    s = pd.Series(np.append(gaps, gaps[-1]))
    cad = s.rolling(window, center=True, min_periods=1).median().bfill().ffill()
    return cad.fillna(60.0).to_numpy()


def worn_intervals(ts: np.ndarray, floor_min: float = 10.0) -> pd.DataFrame:
    """Worn intervals [start_ts, end_ts) from sorted valid-HR timestamps.

    Each sample "covers" half a cadence either side; neighbours closer than gap_max are bridged.
    """
    ts = np.asarray(ts, dtype="int64")
    if ts.size == 0:
        return pd.DataFrame({"start_ts": [], "end_ts": []}, dtype="int64")
    cad = local_cadence(ts)
    gap_max = np.maximum(3 * cad, floor_min * 60)
    breaks = np.flatnonzero(np.diff(ts) > gap_max[:-1])
    starts = np.r_[0, breaks + 1]
    ends = np.r_[breaks, ts.size - 1]
    half = (cad / 2).astype("int64")
    return pd.DataFrame({"start_ts": ts[starts] - half[starts], "end_ts": ts[ends] + half[ends]})


def covered_seconds(intervals: pd.DataFrame, lo: int, hi: int) -> int:
    """Seconds of [lo, hi) covered by the (non-overlapping, sorted) intervals."""
    if intervals.empty:
        return 0
    s = np.clip(intervals["start_ts"].to_numpy(), lo, hi)
    e = np.clip(intervals["end_ts"].to_numpy(), lo, hi)
    return int(np.sum(np.maximum(e - s, 0)))
