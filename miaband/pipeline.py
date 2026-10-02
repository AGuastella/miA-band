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
from .features.recovery import add_recovery_and_readiness
from .features.strain import daily_strain
from .features.timeutil import offset_timeline
from .features.wear import valid_hr, worn_intervals

NIGHT_METRICS = ["tst_min", "spt_min", "waso_min", "sme", "onset_clock", "wake_clock", "nap_min"]
STAGE_METRICS = ["deep_min", "light_min", "rem_min"]


def _context(tables: dict[str, pd.DataFrame], cfg: Config) -> dict:
    sessions = tables.get("sleep_sessions", empty("sleep_sessions"))
    workouts = tables.get("workouts", empty("workouts"))
    hr = valid_hr(tables.get("hr_samples", empty("hr_samples")))
    return dict(sessions=sessions, segments=tables.get("sleep_segments", empty("sleep_segments")),
                workouts=workouts, hr=hr, timeline=offset_timeline(sessions, workouts),
                worn=worn_intervals(hr["ts"].to_numpy(), cfg.sufficiency.wear_gap_min_floor))


def compute_nightly(tables: dict[str, pd.DataFrame], cfg: Config, ctx: dict | None = None) -> pd.DataFrame:
    """Wide frame, one row per wake date from the first to the last night, gaps included."""
    ctx = ctx or _context(tables, cfg)
    sessions, segments, hr, timeline, worn = (ctx[k] for k in ("sessions", "segments", "hr", "timeline", "worn"))
    episodes = build_episodes(sessions, timeline, cfg)
    ep_seg = episode_segments(episodes, segments)
    nights = nightly_sleep(episodes, ep_seg, sessions)
    if nights.empty:
        return pd.DataFrame(columns=["date"])

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


def compute_daily(tables: dict[str, pd.DataFrame], cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Nightly sleep/physiology plus daily strain and ACWR on one calendar, and workout detail."""
    ctx = _context(tables, cfg)
    nightly = compute_nightly(tables, cfg, ctx)
    w = ctx["workouts"]
    firsts, lasts = [], []
    if "episode" in nightly:
        firsts.append(nightly["date"].min()); lasts.append(nightly["date"].max())
    if not w.empty:
        wd = pd.to_datetime(w["start_ts"], unit="s")
        firsts.append(wd.min().normalize()); lasts.append(wd.max().normalize())
    if not ctx["hr"].empty:
        hd = pd.to_datetime(ctx["hr"]["ts"].iloc[[0, -1]], unit="s")
        firsts.append(hd.iloc[0].normalize()); lasts.append(hd.iloc[1].normalize())
    if not firsts:
        return pd.DataFrame(columns=["date"]), pd.DataFrame()
    days = np.arange(np.datetime64(min(firsts), "D"), np.datetime64(max(lasts), "D") + 1, dtype="datetime64[D]")
    rest = (nightly.set_index("date")["rhr_base_center"] if "rhr_base_center" in nightly
            else pd.Series(dtype=float)).reindex(pd.DatetimeIndex(days)).ffill().fillna(cfg.person.resting_hr)
    strain, workouts = daily_strain(days, w, ctx["hr"], ctx["worn"], ctx["timeline"], rest, cfg)
    strain["date"] = pd.to_datetime(strain["date"])
    wide = strain.merge(nightly, on="date", how="left") if "episode" in nightly else strain.assign(episode=np.nan)
    wide = add_recovery_and_readiness(wide.sort_values("date").reset_index(drop=True), cfg)
    return wide, workouts


def detector_recall(tables: dict[str, pd.DataFrame], cfg: Config, since: str | None = None) -> dict:
    """How many recorded workouts with dense HR would the unrecorded-session detector have found
    (>= 50 % overlap) if they had not been recorded? An honesty check on 'detected' loads."""
    from .features.strain import detect_sessions, hrmax_by_date, sustained_peaks, workout_loads
    ctx = _context(tables, cfg)
    w, hr = ctx["workouts"], ctx["hr"]
    if since:
        w = w[w["start_ts"] >= pd.Timestamp(since, tz="UTC").timestamp()]
    if w.empty or hr.empty:
        return {"n": 0}
    days = np.arange(np.datetime64(pd.to_datetime(w["start_ts"].min(), unit="s"), "D"),
                     np.datetime64(pd.to_datetime(w["start_ts"].max(), unit="s"), "D") + 2, dtype="datetime64[D]")
    hrmax = hrmax_by_date(days, w.assign(max_hr=sustained_peaks(w, hr, cfg)), cfg)
    hrmax["date"] = pd.to_datetime(hrmax["date"])
    loads = workout_loads(w, hr, hrmax, ctx["timeline"], cfg)
    dense = loads[loads["method"] == "hr_ours"]
    i, j = np.searchsorted(hr["ts"].to_numpy(), [dense["start_ts"].min() - 86400, dense["end_ts"].max() + 86400]) \
        if not dense.empty else (0, 0)
    det = detect_sessions(hr.iloc[i:j], w.iloc[:0], hrmax, ctx["timeline"], cfg, exclude_recorded=False)
    found = 0
    for r in dense.itertuples(index=False):
        ov = (np.minimum(det["end_ts"], r.end_ts) - np.maximum(det["start_ts"], r.start_ts)).clip(lower=0).sum()
        found += ov >= 0.5 * (r.end_ts - r.start_ts)
    return {"n": len(dense), "found": int(found)}


def to_long(wide: pd.DataFrame) -> pd.DataFrame:
    """Long daily_metrics rows: (date, metric, value, status, reason). Every gap is explicit."""
    rows = []
    for r in wide.itertuples(index=False):
        d = pd.Timestamp(r.date).strftime("%Y-%m-%d")
        def add(metric, value, status, reason=None):
            v = None if value is None or pd.isna(value) else float(value)
            rows.append((d, metric, v if status == "ok" else None, status, reason))
        has_night = not pd.isna(r.episode)
        if hasattr(r, "load_status"):
            for m in ("load", "strain"):
                add(f"strain.{m}", getattr(r, m), r.load_status, r.load_reason)
            add("strain.n_workouts", r.n_workouts, "ok", r.load_method if isinstance(r.load_method, str) else None)
            add("strain.banister", r.banister, "ok" if not pd.isna(r.banister) else "insufficient_data",
                None if not pd.isna(r.banister) else "HR sampled less often than every 2 min")
            add("strain.unrecorded_hint", float(bool(r.unrecorded_hint)), "ok")
            add("strain.hr_max", r.hr_max, "ok", r.hr_max_basis)
            add("acwr.ewma", r.acwr, r.acwr_status, r.acwr_reason)
            add("acwr.rolling", r.acwr_rolling, r.acwr_status, r.acwr_reason)
        if hasattr(r, "recovery_status"):
            add("recovery.score", r.recovery, r.recovery_status, r.recovery_reason)
            for c in ("rhr", "sleep"):
                if hasattr(r, f"recovery_z_{c}"):
                    add(f"recovery.z_{c}", getattr(r, f"recovery_z_{c}"), r.recovery_status, r.recovery_reason)
                    add(f"recovery.pts_{c}", getattr(r, f"recovery_pts_{c}"), r.recovery_status, r.recovery_reason)
            if r.readiness_rule is not None and not pd.isna(r.readiness_rule):
                add("readiness.rule", r.readiness_rule, "ok", r.readiness)
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
        add("sleep.sri_7d", r.sri, "ok" if not pd.isna(r.sri) else "insufficient_data", r.sri_reason)
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
            add(f"{pre}.spread28", getattr(r, f"{pre}_base_spread"), st, why)
            add(f"{pre}.mean7", getattr(r, f"{pre}_mean7"),
                "ok" if not pd.isna(getattr(r, f"{pre}_mean7")) else "insufficient_data")
    return pd.DataFrame(rows, columns=["date", "metric", "value", "status", "reason"])


def load_tables(con: sqlite3.Connection) -> dict[str, pd.DataFrame]:
    return {name: store.read_table(con, name)
            for name in ("hr_samples", "sleep_sessions", "sleep_segments", "workouts")}


def run(con: sqlite3.Connection, cfg: Config) -> pd.DataFrame:
    wide, _ = compute_daily(load_tables(con), cfg)
    if not wide.empty:
        store.write_daily(con, to_long(wide))
    return wide
