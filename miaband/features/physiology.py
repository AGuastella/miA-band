"""Nightly resting HR (docs/SPEC.md §5.1).

RHR = lowest rolling *mean* HR over a time window W inside the main sleep, skipping the first
`rhr_skip_onset_min` (sleep-onset transients). W = max(rhr_window_min, 3 x cadence) so a window
holds >= ~3 samples at sparse sampling; a window counts only if it holds >= `rhr_window_min_fill`
of the samples expected at that cadence. The nocturnal nadir is the least confounded RHR a wrist
device can give; the rolling mean protects against single-sample optical artifacts.

Night is `insufficient_data` when HR covers < `night_hr_coverage_min` of the sleep period.
HRV is not computed: neither source provides beat-to-beat intervals (SPEC §9.2).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..config import Config
from .wear import covered_seconds


def nightly_rhr(nights: pd.DataFrame, hr: pd.DataFrame, worn: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    p, suff = cfg.physiology, cfg.sufficiency
    ts_all = hr["ts"].to_numpy(dtype="int64") if not hr.empty else np.zeros(0, "int64")
    bpm_all = hr["bpm"].to_numpy(dtype=float) if not hr.empty else np.zeros(0)
    rows = []
    for n in nights.itertuples(index=False):
        dur = n.wake_ts - n.onset_ts
        coverage = covered_seconds(worn, n.onset_ts, n.wake_ts) / dur if dur > 0 else 0.0
        row = dict(date=n.date, hr_coverage=coverage, rhr=np.nan, sleep_hr_mean=np.nan,
                   rhr_window_min=np.nan, hr_cadence_s=np.nan)
        lo = n.onset_ts + p.rhr_skip_onset_min * 60
        i, j = np.searchsorted(ts_all, [lo, n.wake_ts])
        ts, bpm = ts_all[i:j], bpm_all[i:j]
        if coverage < suff.night_hr_coverage_min or len(ts) < 3:
            row["phys_status"] = "insufficient_data"
            row["phys_reason"] = f"HR covers {coverage:.0%} of the sleep period"
            rows.append(row)
            continue
        cadence = float(np.median(np.diff(ts)))
        window_s = max(p.rhr_window_min * 60, 3 * cadence)
        min_count = max(2, math.ceil(p.rhr_window_min_fill * window_s / cadence))
        s = pd.Series(bpm, index=pd.to_datetime(ts, unit="s"))
        roll = s.rolling(f"{int(window_s)}s", min_periods=min_count).mean()
        # A time-based window is right-aligned: drop windows that start before `lo`.
        roll = roll[roll.index >= pd.to_datetime(lo + window_s - cadence, unit="s")]
        row.update(hr_cadence_s=cadence, rhr_window_min=window_s / 60,
                   sleep_hr_mean=float(bpm.mean()))
        if roll.notna().any():
            row["rhr"] = float(roll.min())
            row["phys_status"], row["phys_reason"] = "ok", None
        else:
            row["phys_status"] = "insufficient_data"
            row["phys_reason"] = "no window with enough samples"
        rows.append(row)
    return pd.DataFrame(rows)
