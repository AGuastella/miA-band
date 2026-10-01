"""Local time from recorded UTC offsets (docs/SPEC.md §2.3, §9.2).

Devices store, on every sleep and workout record, the UTC offset in force at the time. We use
those offsets rather than one configured zone: the person has lived in, and travelled across,
several zones. Any instant takes the offset of the nearest record within `max_gap_h`; beyond
that (e.g. today, before tonight's sleep is synced) the configured fallback zone applies, with
its DST rules.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

DAY = 86400


def offset_timeline(*frames: pd.DataFrame) -> pd.DataFrame:
    """(ts, offset_min) anchor points from any frames with start_ts/end_ts/tz_offset_min."""
    parts = []
    for df in frames:
        if df is None or df.empty:
            continue
        d = df.dropna(subset=["tz_offset_min"])
        for col in ("start_ts", "end_ts"):
            parts.append(pd.DataFrame({"ts": d[col].astype("int64"),
                                       "offset_min": d["tz_offset_min"].astype("int64")}))
    if not parts:
        return pd.DataFrame({"ts": pd.Series(dtype="int64"), "offset_min": pd.Series(dtype="int64")})
    tl = pd.concat(parts).drop_duplicates("ts").sort_values("ts", kind="stable")
    return tl.reset_index(drop=True)


def zone_offsets(ts: np.ndarray, tz: ZoneInfo) -> np.ndarray:
    """Offset (minutes) of zone `tz` at each UTC instant, DST-aware."""
    ts = np.asarray(ts, dtype="int64")
    if ts.size == 0:
        return np.zeros(0, dtype="int64")
    idx = pd.to_datetime(ts, unit="s", utc=True).tz_convert(tz)
    local_naive = idx.tz_localize(None)
    utc_naive = pd.to_datetime(ts, unit="s")
    return np.array((local_naive - utc_naive) // pd.Timedelta(minutes=1), dtype="int64")


def local_offsets(ts, timeline: pd.DataFrame, fallback_tz: ZoneInfo, max_gap_h: float = 36) -> np.ndarray:
    ts = np.asarray(ts, dtype="int64")
    out = zone_offsets(ts, fallback_tz).copy()
    if timeline.empty or ts.size == 0:
        return out
    anchors = timeline["ts"].to_numpy(dtype="int64")
    offs = timeline["offset_min"].to_numpy(dtype="int64")
    right = np.searchsorted(anchors, ts, side="left").clip(0, len(anchors) - 1)
    left = (right - 1).clip(0, len(anchors) - 1)
    pick = np.where(np.abs(anchors[left] - ts) <= np.abs(anchors[right] - ts), left, right)
    near = np.abs(anchors[pick] - ts) <= max_gap_h * 3600
    out[near] = offs[pick[near]]
    return out


def local_seconds(ts, offsets) -> np.ndarray:
    """Seconds since 1970-01-01 *local wall clock* (useful for date and clock-time maths)."""
    return np.asarray(ts, dtype="int64") + np.asarray(offsets, dtype="int64") * 60


def local_date(ts, offsets) -> np.ndarray:
    return (local_seconds(ts, offsets) // DAY).astype("datetime64[D]")


def local_clock_min(ts, offsets) -> np.ndarray:
    """Minutes after local midnight, 0..1439."""
    return (local_seconds(ts, offsets) % DAY) // 60
