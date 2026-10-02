"""Training load, daily strain and ACWR (docs/SPEC.md §6, §9.2).

Primary load: Edwards' TRIMP (Edwards 1993) per workout = sum of minutes in five %HRmax zones
(50-60, 60-70, 70-80, 80-90, 90-100 %) weighted 1..5. Minutes are computed by us from the HR
samples inside the workout window when HR is dense enough (<= 2-min cadence covering >= 80 % of
the workout): same definition for every device and year. Otherwise the band's own zone
durations are used (method 'device_zones', documented as such); otherwise the workout's load is
unknown and its day is 'insufficient_data'. Nothing is approximated from averages.

Daily load = sum of that local day's workouts. A day without workouts is a real 0 only if the
band was worn for most of the day (`day_wear_window`), else unknown. When HR is too sparse for
the Banister check, that 0 carries a reason ("unverified") and is shown as 0*.

Sessions not started on the band are detected in dense background HR (sustained HR >= 60 %
HRmax for >= 20 min, outside recorded workouts) and get the same Edwards load, method
'detected'. `miaband sanity` reports the detector's recall on recorded workouts.

Secondary: Banister TRIMP over all daytime HR (>= 30 % HR reserve) on days with <= 2-min
cadence, used to hint at sessions not started on the band. Not an input to anything.

Strain (0-21) = 21 * (1 - exp(-load / tau)): a readability transform, not physiology and not
WHOOP's strain. tau = "auto" puts the median workout day of the most recent 365 days at 12/21.

ACWR: EWMA (Williams et al. 2017), acute 7 d / chronic 28 d, lambda = 2/(N+1), uncoupled. A
missing day leaves both averages unchanged (no imputation) and is counted; too many missing
days, a short history or a tiny chronic load make the ratio 'insufficient_data'. Descriptive
only: the injury-risk evidence for ACWR is weak and contested (Impellizzeri et al. 2020).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..config import Config
from .nights import record_offsets
from .timeutil import DAY, local_date, local_offsets
from .wear import covered_seconds

ZONE_EDGES = (0.5, 0.6, 0.7, 0.8, 0.9)        # lower bounds of zones 1..5 as fraction of HRmax
BANISTER = {"male": (0.64, 1.92), "female": (0.86, 1.67)}


def _day_index(days) -> np.ndarray:
    return np.asarray(days).astype("datetime64[D]")


# ---------------------------------------------------------------------------------------
def hrmax_by_date(days: np.ndarray, workouts: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """HRmax per date: config value, else max(220 - age at that date, observed), where observed
    is the median of the 3 highest *sustained* workout peaks (column `max_hr`, see
    sustained_peaks) in the lookback window, ignoring values above 220 - age + margin."""
    s = cfg.strain
    days = _day_index(days)
    age = np.array([cfg.person.age_on(d.astype(object)) for d in days])
    predicted = 220.0 - age
    if cfg.person.hr_max:
        return pd.DataFrame({"date": days, "hr_max": float(cfg.person.hr_max), "hr_max_basis": "config"})
    w = workouts.dropna(subset=["max_hr"])
    w_day = _day_index((w["start_ts"].to_numpy(dtype="int64") // DAY).astype("datetime64[D]"))
    w_max = w["max_hr"].to_numpy(dtype=float)
    hr, basis = [], []
    for d, pred in zip(days, predicted):
        m = (w_day <= d) & (w_day > d - np.timedelta64(s.hrmax_lookback_days, "D")) & (w_max <= pred + s.hrmax_artifact_margin)
        top = np.sort(w_max[m])[-3:]
        obs = float(np.median(top)) if len(top) == 3 else np.nan
        if not np.isnan(obs) and obs > pred:
            hr.append(obs); basis.append("observed")
        else:
            hr.append(pred); basis.append("age")
    return pd.DataFrame({"date": days, "hr_max": hr, "hr_max_basis": basis})


def sustained_peaks(workouts: pd.DataFrame, hr: pd.DataFrame, cfg: Config) -> pd.Series:
    """Per workout: highest 3-sample rolling median of our HR inside the workout, only where HR
    is dense (<= workout_max_cadence_s). The band's own `max_hr` is a single-sample peak and is
    not used: optical spikes during arm-heavy play would inflate HRmax."""
    ts_all = hr["ts"].to_numpy(dtype="int64")
    bpm_all = hr["bpm"].to_numpy(dtype=float)
    out = []
    for r in workouts.itertuples(index=False):
        i, j = np.searchsorted(ts_all, [r.start_ts, r.end_ts])
        ts, bpm = ts_all[i:j], bpm_all[i:j]
        if len(ts) >= 5 and np.median(np.diff(ts)) <= cfg.strain.workout_max_cadence_s:
            out.append(float(pd.Series(bpm).rolling(3).median().max()))
        else:
            out.append(np.nan)
    return pd.Series(out, index=workouts.index, dtype=float)


def zone_minutes(ts: np.ndarray, bpm: np.ndarray, hr_max: float, max_dt: float, end_ts: int) -> np.ndarray:
    """Minutes in Edwards zones 1..5. Each sample holds until the next one (the last until the
    workout ends), capped at max_dt so a gap is not credited to the previous sample."""
    if len(ts) == 0:
        return np.zeros(5)
    dt = np.diff(np.append(ts, max(end_ts, ts[-1]))).astype(float)
    dt = np.minimum(dt, max_dt) / 60
    frac = bpm / hr_max
    zone = np.searchsorted(ZONE_EDGES, frac, side="right")      # 0 = below zone 1
    return np.array([dt[zone == z].sum() for z in range(1, 6)])


def edwards(zmin) -> float:
    return float(np.dot(zmin, [1, 2, 3, 4, 5]))


def workout_loads(workouts: pd.DataFrame, hr: pd.DataFrame, hrmax: pd.DataFrame,
                  timeline: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    s = cfg.strain
    cols = ["start_ts", "end_ts", "sport", "date", "load", "method", "load_ours", "load_device",
            "hr_max", "cadence_s", "coverage"]
    if workouts.empty:
        return pd.DataFrame(columns=cols)
    w = workouts.sort_values("start_ts").reset_index(drop=True)
    off = record_offsets(w, "start_ts", timeline, cfg)
    w["date"] = local_date(w["start_ts"], off)
    hm = hrmax.set_index("date")["hr_max"]
    ts_all = hr["ts"].to_numpy(dtype="int64")
    bpm_all = hr["bpm"].to_numpy(dtype=float)
    rows = []
    for r in w.itertuples(index=False):
        hr_max = float(hm.get(pd.Timestamp(r.date), np.nan))
        i, j = np.searchsorted(ts_all, [r.start_ts, r.end_ts])
        ts, bpm = ts_all[i:j], bpm_all[i:j]
        dur = r.end_ts - r.start_ts
        cadence = float(np.median(np.diff(ts))) if len(ts) > 2 else np.inf
        coverage = min(1.0, len(ts) * cadence / dur) if np.isfinite(cadence) and dur > 0 else 0.0
        ours = np.nan
        if cadence <= s.workout_max_cadence_s and coverage >= s.workout_min_coverage and not np.isnan(hr_max):
            ours = edwards(zone_minutes(ts, bpm, hr_max, 2 * cadence, r.end_ts))
        zs = np.array([getattr(r, f"zone{z}_s") for z in range(1, 6)], dtype=float)
        device = edwards(zs / 60) if not np.isnan(zs).any() and zs.sum() > 0 else np.nan
        if not np.isnan(ours):
            load, method = ours, "hr_ours"
        elif not np.isnan(device):
            load, method = device, "device_zones"
        else:
            load, method = np.nan, "low_resolution"
        rows.append(dict(start_ts=r.start_ts, end_ts=r.end_ts, sport=r.sport, date=r.date, load=load,
                         method=method, load_ours=ours, load_device=device, hr_max=hr_max,
                         cadence_s=cadence, coverage=coverage, avg_hr=r.avg_hr,
                         **{f"zone{z}_s": zs[z - 1] for z in range(1, 6)}))
    return pd.DataFrame(rows)


def detect_sessions(hr: pd.DataFrame, workouts: pd.DataFrame, hrmax: pd.DataFrame,
                    timeline: pd.DataFrame, cfg: Config, exclude_recorded: bool = True) -> pd.DataFrame:
    """Training sessions not started on the band, found in dense (<= 2-min) background HR.

    A sample is 'active' when the 5-sample rolling median HR is >= detect_threshold x HRmax.
    Active runs closer than detect_merge_gap_min are merged; runs of >= detect_min_minutes
    are sessions. Recorded workouts (+-5 min) are masked out so nothing is counted twice.
    The session's load is the same Edwards TRIMP as for recorded workouts.
    """
    s = cfg.strain
    cols = ["start_ts", "end_ts", "sport", "date", "load", "method", "load_ours", "load_device",
            "hr_max", "cadence_s", "coverage", "avg_hr"]
    if hr.empty or not s.detect_sessions:
        return pd.DataFrame(columns=cols)
    ts = hr["ts"].to_numpy(dtype="int64")
    bpm = hr["bpm"].to_numpy(dtype=float)
    off = local_offsets(ts, timeline, cfg.tz)
    dates = local_date(ts, off)
    hm = hrmax.set_index("date")["hr_max"]
    frac = bpm / hm.reindex(pd.DatetimeIndex(dates)).to_numpy()
    gap = np.diff(ts, prepend=ts[0] - 60)
    seg = np.cumsum(gap > s.workout_max_cadence_s)            # break smoothing at sparse gaps
    smooth = (pd.Series(frac).groupby(seg).transform(
        lambda x: x.rolling(5, center=True, min_periods=3).median())).to_numpy()
    active = (smooth >= s.detect_threshold) & (gap <= s.workout_max_cadence_s)
    if exclude_recorded and not workouts.empty:
        for a, b in zip(workouts["start_ts"].to_numpy(), workouts["end_ts"].to_numpy()):
            i, j = np.searchsorted(ts, [a - 300, b + 300])
            active[i:j] = False
    idx = np.flatnonzero(active)
    if idx.size == 0:
        return pd.DataFrame(columns=cols)
    runs, start, prev = [], idx[0], idx[0]
    for k in idx[1:]:
        if ts[k] - ts[prev] > s.detect_merge_gap_min * 60:
            runs.append((start, prev)); start = k
        prev = k
    runs.append((start, prev))
    rows = []
    for a, b in runs:
        if ts[b] - ts[a] < s.detect_min_minutes * 60:
            continue
        t, h = ts[a:b + 1], bpm[a:b + 1]
        cadence = float(np.median(np.diff(t)))
        hmax = float(hm.get(pd.Timestamp(dates[a]), np.nan))
        load = edwards(zone_minutes(t, h, hmax, 2 * cadence, int(t[-1] + cadence)))
        rows.append(dict(start_ts=int(t[0]), end_ts=int(t[-1] + cadence), sport="detected", date=dates[a],
                         load=load, method="detected", load_ours=load, load_device=np.nan, hr_max=hmax,
                         cadence_s=cadence, coverage=1.0, avg_hr=float(h.mean())))
    return pd.DataFrame(rows, columns=cols)


# ---------------------------------------------------------------------------------------
def day_wear(days: np.ndarray, worn: pd.DataFrame, timeline: pd.DataFrame, cfg: Config) -> np.ndarray:
    """Share of the local `day_wear_window` hours covered by wear, per day."""
    h0, h1 = cfg.strain.day_wear_window
    days = _day_index(days)
    noon = days.astype("datetime64[s]").astype("int64") + 12 * 3600
    off = local_offsets(noon, timeline, cfg.tz) * 60
    lo = days.astype("datetime64[s]").astype("int64") + h0 * 3600 - off
    hi = days.astype("datetime64[s]").astype("int64") + h1 * 3600 - off
    if worn.empty:
        return np.zeros(len(days))
    ws, we = worn["start_ts"].to_numpy(), worn["end_ts"].to_numpy()
    out = np.empty(len(days))
    for k, (a, b) in enumerate(zip(lo, hi)):
        i = max(np.searchsorted(we, a) - 1, 0)
        j = np.searchsorted(ws, b)
        out[k] = covered_seconds(worn.iloc[i:j], int(a), int(b)) / (b - a)
    return out


def banister_daily(days: np.ndarray, hr: pd.DataFrame, hrmax: pd.DataFrame, hr_rest: pd.Series,
                   timeline: pd.DataFrame, cfg: Config) -> pd.Series:
    """Banister TRIMP per local day over all HR at >= hrr_floor reserve (secondary)."""
    s = cfg.strain
    a, b = BANISTER[cfg.person.sex]
    if hr.empty:
        return pd.Series(np.nan, index=pd.DatetimeIndex(_day_index(days)))
    ts = hr["ts"].to_numpy(dtype="int64")
    d = pd.DataFrame({"date": local_date(ts, local_offsets(ts, timeline, cfg.tz)),
                      "bpm": hr["bpm"].to_numpy(dtype=float),
                      "dt": np.diff(np.append(ts, ts[-1] + 60)).astype(float)})
    cadence = d.groupby("date")["dt"].median()
    d["dt"] = d["dt"].clip(upper=2 * s.banister_max_cadence_s) / 60
    d = d.merge(hrmax[["date", "hr_max"]], on="date", how="left")
    d["rest"] = d["date"].map(hr_rest)
    hrr = ((d["bpm"] - d["rest"]) / (d["hr_max"] - d["rest"])).clip(0, 1)
    d["trimp"] = np.where(hrr >= s.hrr_floor, d["dt"] * hrr * a * np.exp(b * hrr), 0.0)
    out = d.groupby("date")["trimp"].sum()
    out[cadence.reindex(out.index) > s.banister_max_cadence_s] = np.nan
    return out.reindex(pd.DatetimeIndex(_day_index(days)))


def ewma_acwr(days: np.ndarray, load: np.ndarray, cfg: Config) -> pd.DataFrame:
    """EWMA and rolling ACWR with explicit missing-day handling. load: NaN = unknown day."""
    s = cfg.strain
    la, lc = 2 / (s.acwr_acute_days + 1), 2 / (s.acwr_chronic_days + 1)
    acute = chronic = None
    started = None
    rows = []
    known = ~np.isnan(load)
    for k, (d, x) in enumerate(zip(_day_index(days), load)):
        if not np.isnan(x):
            if acute is None:
                acute = chronic = 0.0
                started = k
            acute = la * x + (1 - la) * acute
            chronic = lc * x + (1 - lc) * chronic
        miss7 = int((~known[max(0, k - 6):k + 1]).sum())
        miss28 = int((~known[max(0, k - 27):k + 1]).sum())
        win28 = load[max(0, k - 27):k + 1]
        win7 = load[max(0, k - 6):k + 1]
        row = dict(date=d, acute=acute, chronic=chronic, acwr=np.nan, acwr_rolling=np.nan,
                   missing_7=miss7, missing_28=miss28)
        if started is None or k - started + 1 < s.acwr_chronic_days:
            row["acwr_status"], row["acwr_reason"] = "warming_up", "needs 28 days of load history"
        elif miss7 > s.acwr_max_missing_7 or miss28 > s.acwr_max_missing_28:
            row["acwr_status"] = "insufficient_data"
            row["acwr_reason"] = f"{miss7} unknown days in last 7, {miss28} in last 28"
        elif chronic < s.acwr_min_chronic:
            row["acwr_status"], row["acwr_reason"] = "insufficient_data", "chronic load too small for a stable ratio"
        else:
            row["acwr"] = acute / chronic
            row["acwr_rolling"] = np.nanmean(win7) / np.nanmean(win28)
            row["acwr_status"], row["acwr_reason"] = "ok", None
        rows.append(row)
    return pd.DataFrame(rows)


def resolve_tau(daily_load: pd.Series, cfg: Config) -> float:
    if cfg.strain.tau != "auto":
        return float(cfg.strain.tau)
    # calibrate on the most recent year: training habits change a lot over a decade
    pos = daily_load.iloc[-365:]
    pos = pos[pos > 0].dropna()
    if pos.empty:
        return 150.0
    # median workout day -> strain 12:  1 - exp(-m / tau) = 12/21
    return float(pos.median() / -math.log(1 - 12 / 21))


def strain_score(load: np.ndarray, tau: float) -> np.ndarray:
    return 21.0 * (1.0 - np.exp(-np.asarray(load, dtype=float) / tau))


def daily_strain(days: np.ndarray, workouts: pd.DataFrame, hr: pd.DataFrame, worn: pd.DataFrame,
                 timeline: pd.DataFrame, hr_rest: pd.Series, cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-day load/strain/ACWR frame and the per-workout detail frame."""
    days = _day_index(days)
    peaks = workouts.assign(max_hr=sustained_peaks(workouts, hr, cfg)) if not workouts.empty else workouts
    hrmax = hrmax_by_date(days, peaks, cfg)
    hrmax["date"] = pd.to_datetime(hrmax["date"])
    wl = workout_loads(workouts, hr, hrmax, timeline, cfg)
    detected = detect_sessions(hr, workouts, hrmax, timeline, cfg)
    if not detected.empty:
        wl = pd.concat([wl, detected], ignore_index=True).sort_values("start_ts", ignore_index=True)
    wear = day_wear(days, worn, timeline, cfg)
    idx = pd.DatetimeIndex(days)
    by_day = wl.groupby(pd.to_datetime(wl["date"]))if not wl.empty else None
    n_workouts = by_day.size().reindex(idx, fill_value=0) if by_day is not None else pd.Series(0, index=idx)
    unknown = (by_day["load"].apply(lambda x: x.isna().any()).reindex(idx, fill_value=False)
               if by_day is not None else pd.Series(False, index=idx))
    load = (by_day["load"].sum(min_count=1).reindex(idx) if by_day is not None else pd.Series(np.nan, index=idx))
    methods = (by_day["method"].agg(lambda m: ",".join(sorted(set(m)))).reindex(idx)
               if by_day is not None else pd.Series(None, index=idx, dtype=object))

    status = np.full(len(days), "ok", dtype=object)
    reason = np.full(len(days), None, dtype=object)
    rest_day = (n_workouts.to_numpy() == 0)
    worn_enough = wear >= cfg.strain.day_wear_min
    load = load.to_numpy(dtype=float, copy=True)
    load[rest_day & worn_enough] = 0.0
    status[rest_day & ~worn_enough] = "insufficient_data"
    reason[rest_day & ~worn_enough] = "no workout recorded and band worn < 70 % of 08-22"
    bad = unknown.to_numpy()
    load[bad] = np.nan
    status[bad] = "insufficient_data"
    reason[bad] = "a workout has neither dense HR nor band zone data"

    banister = banister_daily(days, hr, hrmax, hr_rest, timeline, cfg).to_numpy()
    unverified = rest_day & worn_enough & np.isnan(banister)
    reason[unverified] = "no workout recorded; HR too sparse to rule out an unrecorded session"
    tau = resolve_tau(pd.Series(load), cfg)
    acwr = ewma_acwr(days, load, cfg)
    out = pd.DataFrame({
        "date": days, "load": load, "strain": strain_score(load, tau), "load_status": status,
        "load_reason": reason, "n_workouts": n_workouts.to_numpy(), "load_method": methods.to_numpy(),
        "day_wear": wear, "banister": banister, "hr_max": hrmax["hr_max"].to_numpy(),
        "hr_max_basis": hrmax["hr_max_basis"].to_numpy(), "tau": tau})
    out["unrecorded_hint"] = (out["n_workouts"] == 0) & (out["banister"] >= cfg.strain.unrecorded_hint_banister)
    out = out.merge(acwr.drop(columns="date").assign(date=days), on="date")
    return out, wl
