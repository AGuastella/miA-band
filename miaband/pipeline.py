"""Raw canonical tables -> per-day metrics. Pure: no I/O; `run` wires it to the store."""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from . import store
from .canonical import empty
from .config import Config
from .features.baselines import trailing_baseline
from .features.nights import build_episodes, episode_segments
from .features.physiology import nightly_rhr
from .features.sleep import nightly_sleep, regularity, sleep_wake_grid
from .features.timeutil import offset_timeline
from .features.wear import valid_hr, worn_intervals

NIGHT_METRICS = ["tst_min", "spt_min", "waso_min", "sme", "onset_clock", "wake_clock", "nap_min"]
STAGE_METRICS = ["deep_min", "light_min", "rem_min"]


def compute_nightly(tables: dict[str, pd.DataFrame], cfg: Config) -> pd.DataFrame:
    """Wide frame, one row per wake date from the first to the last night, gaps included."""
    sessions = tables.get("sleep_sessions", empty("sleep_sessions"))
    segments = tables.get("sleep_segments", empty("sleep_segments"))
    workouts = tables.get("workouts", empty("workouts"))
    hr = valid_hr(tables.get("hr_samples", empty("hr_samples")))

    timeline = offset_timeline(sessions, workouts)
    episodes = build_episodes(sessions, timeline, cfg)
    ep_seg = episode_segments(episodes, segments)
    nights = nightly_sleep(episodes, ep_seg, sessions)
    if nights.empty:
        return pd.DataFrame(columns=["date"])
    worn = worn_intervals(hr["ts"].to_numpy(), cfg.sufficiency.wear_gap_min_floor)

    phys = nightly_rhr(nights, hr, worn, cfg)
    grid, day0 = sleep_wake_grid(episodes, ep_seg, worn, timeline, cfg)
    reg = regularity(nights, grid, day0, cfg)
    wide = nights.merge(phys, on="date", how="left").merge(reg, on="date", how="left")

    floors = cfg.baselines.spread_floor
    for metric, col, bad, floor in (("rhr", "rhr", +1, floors["rhr"]),
                                    ("tst", "tst_min", -1, floors["tst_min"]),
                                    ("sleep_hr", "sleep_hr_mean", +1, floors["sleep_hr_mean"])):
        vals = wide[col].where(wide["phys_status"] == "ok") if col != "tst_min" else wide[col]
        bl = trailing_baseline(wide["date"].to_numpy(), vals.to_numpy(), wide["device_id"].to_numpy(),
                               floor=floor, bad_direction=bad, cfg=cfg)
        wide = wide.merge(bl.add_prefix(f"{metric}_").rename(columns={f"{metric}_date": "date"}), on="date")

    full = pd.DataFrame({"date": np.arange(wide["date"].min(), wide["date"].max() + np.timedelta64(1, "D"),
                                           dtype="datetime64[D]")})
    return full.merge(wide, on="date", how="left")


def to_long(wide: pd.DataFrame) -> pd.DataFrame:
    """Long daily_metrics rows: (date, metric, value, status, reason). Every gap is explicit."""
    rows = []
    for r in wide.itertuples(index=False):
        d = pd.Timestamp(r.date).strftime("%Y-%m-%d")
        has_night = not pd.isna(r.episode)
        def add(metric, value, status, reason=None):
            v = None if value is None or pd.isna(value) else float(value)
            rows.append((d, metric, v if status == "ok" else None, status, reason))
        if not has_night:
            for m in NIGHT_METRICS + STAGE_METRICS + ["rhr", "sleep_hr_mean"]:
                add(f"sleep.{m}" if m not in ("rhr", "sleep_hr_mean") else f"phys.{m}", None,
                    "insufficient_data", "no main sleep recorded (not worn, or no sleep >= threshold)")
            continue
        for m in NIGHT_METRICS:
            add(f"sleep.{m}", getattr(r, m), "ok")
        for m in STAGE_METRICS:
            add(f"sleep.{m}", getattr(r, m), "ok" if r.stages_available else "not_available",
                None if r.stages_available else "device produced no REM staging for this night")
        add("sleep.sri_7d", r.sri, "ok" if not pd.isna(r.sri) else "insufficient_data",
            None if not pd.isna(r.sri) else "fewer than the required valid day pairs")
        for m in ("onset_csd", "wake_csd"):
            add(f"sleep.{m}_7d", getattr(r, m), r.reg_status, None if r.reg_status == "ok" else "too few nights in window")
        add("phys.hr_coverage", r.hr_coverage, "ok")
        for m in ("rhr", "sleep_hr_mean"):
            add(f"phys.{m}", getattr(r, m), r.phys_status, r.phys_reason)
        for pre in ("rhr", "tst", "sleep_hr"):
            st = getattr(r, f"{pre}_base_status")
            if pre != "tst" and r.phys_status != "ok":
                st, why = r.phys_status, r.phys_reason
            else:
                why = None if st == "ok" else f"baseline needs more valid nights ({getattr(r, f'{pre}_base_n')} so far)"
            add(f"{pre}.z", getattr(r, f"{pre}_z"), st, why)
            add(f"{pre}.flag", float(bool(getattr(r, f"{pre}_flag"))), st, why)
            add(f"{pre}.base28", getattr(r, f"{pre}_base_center"), st, why)
            add(f"{pre}.mean7", getattr(r, f"{pre}_mean7"),
                "ok" if not pd.isna(getattr(r, f"{pre}_mean7")) else "insufficient_data")
    return pd.DataFrame(rows, columns=["date", "metric", "value", "status", "reason"])


def load_tables(con: sqlite3.Connection) -> dict[str, pd.DataFrame]:
    return {name: store.read_table(con, name)
            for name in ("hr_samples", "sleep_sessions", "sleep_segments", "workouts")}


def run(con: sqlite3.Connection, cfg: Config) -> pd.DataFrame:
    wide = compute_nightly(load_tables(con), cfg)
    if not wide.empty and "episode" in wide:
        store.write_daily(con, to_long(wide))
    return wide
