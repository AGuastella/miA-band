"""Trailing personal baselines, z-scores and deviation flags (docs/SPEC.md §5.3).

For date D, the long baseline uses valid values from the `long_days` calendar days *before* D
(D itself excluded), restricted to the same device era as D: a sensor/algorithm change shifts RHR
and sleep by a few units and must not read as a deviation. Robust statistics (median, 1.4826*MAD
with a floor) because n is small and one sick night must not inflate the spread.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Config


def trailing_baseline(dates: np.ndarray, values: np.ndarray, eras: np.ndarray, *, floor: float,
                      bad_direction: int, cfg: Config) -> pd.DataFrame:
    """bad_direction: +1 if high values are unfavourable (RHR), -1 if low ones are (TST)."""
    b = cfg.baselines
    dates = np.asarray(dates, dtype="datetime64[D]")
    values = np.asarray(values, dtype=float)
    eras = np.asarray(eras)
    out = []
    for d, x, era in zip(dates, values, eras):
        long_mask = (dates < d) & (dates >= d - np.timedelta64(b.long_days, "D")) & (eras == era) & ~np.isnan(values)
        short_mask = (dates < d) & (dates >= d - np.timedelta64(b.short_days, "D")) & (eras == era) & ~np.isnan(values)
        lv = values[long_mask]
        row = dict(date=d, base_n=len(lv), base_center=np.nan, base_spread=np.nan, z=np.nan,
                   flag=False, mean7=np.nan)
        if short_mask.sum() >= b.short_min_n:
            row["mean7"] = float(values[short_mask].mean())
        if len(lv) < b.long_min_n:
            row["base_status"] = "warming_up"
        else:
            center = float(np.median(lv))
            spread = max(1.4826 * float(np.median(np.abs(lv - center))), floor)
            row.update(base_center=center, base_spread=spread, base_status="ok")
            if not np.isnan(x):
                z = (x - center) / spread
                row["z"] = z
                row["flag"] = bool(z * bad_direction >= b.flag_sd)
        out.append(row)
    return pd.DataFrame(out)
