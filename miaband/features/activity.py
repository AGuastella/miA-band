"""Daily step counts.

The band writes one row per *active* minute, so a worn day with no rows is a real 0, and a day the
band was not worn is unknown. Several devices can overlap on switch days (or a phone may have
been counting too): per day we take the device with the most steps, never the sum, so the same
walk is not counted twice.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Config
from .timeutil import local_date, local_offsets


def daily_steps(days: np.ndarray, steps: pd.DataFrame, wear: np.ndarray, timeline: pd.DataFrame,
                cfg: Config) -> pd.DataFrame:
    days = np.asarray(days).astype("datetime64[D]")
    idx = pd.DatetimeIndex(days)
    if steps.empty:
        per_day = pd.Series(np.nan, index=idx)
    else:
        ts = steps["ts"].to_numpy(dtype="int64")
        d = pd.DataFrame({"date": pd.DatetimeIndex(local_date(ts, local_offsets(ts, timeline, cfg.tz))),
                          "device_id": steps["device_id"].to_numpy(), "steps": steps["steps"].to_numpy(dtype=float)})
        per_day = d.groupby(["date", "device_id"])["steps"].sum().groupby(level="date").max().reindex(idx)
    has_rows = per_day.notna().to_numpy()
    worn = wear >= cfg.strain.day_wear_min
    value = np.where(has_rows, per_day.to_numpy(), np.where(worn, 0.0, np.nan))
    status = np.where(has_rows | worn, "ok", "insufficient_data")
    reason = np.where(has_rows & ~worn, "band off for part of 08-22: steps may be undercounted",
                      np.where(~has_rows & ~worn, "band not worn: steps unknown", None))
    return pd.DataFrame({"date": days, "steps": value, "steps_status": status, "steps_reason": reason})
