"""Strain: Edwards per workout (ours vs band zones), daily load rules, HRmax, ACWR."""
import dataclasses
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from miaband.config import Config
from miaband.features.strain import ewma_acwr, hrmax_by_date, zone_minutes
from miaband.pipeline import compute_daily
from miaband.sources.synthetic import Scenario, generate

D = dt.date
CFG = Config()


def daily(sc: Scenario, mutate=None, cfg: Config = CFG):
    batch, _ = generate(sc)
    if mutate:
        mutate(batch.tables)
    w, wl = compute_daily(batch.tables, cfg)
    return w.set_index(w["date"].dt.date), wl


def every_other_day(start, n, bpm=150, minutes=90):
    return {start + dt.timedelta(days=i): (dt.time(18), minutes, bpm) for i in range(0, n, 2)}


def test_zone_minutes_last_sample_runs_to_workout_end():
    ts = np.arange(0, 90 * 60, 60)
    z = zone_minutes(ts, np.full(len(ts), 150.0), 191.0, 120, 90 * 60)
    assert z.tolist() == [0, 0, 90, 0, 0]                 # 150/191 = 78.5 % -> zone 3


def test_edwards_from_dense_hr_matches_band_zones():
    w, wl = daily(Scenario(first_wake=D(2026, 3, 1), nights=10, workouts=every_other_day(D(2026, 3, 1), 10)))
    assert (wl["method"] == "hr_ours").all()
    assert wl["load_ours"].tolist() == wl["load_device"].tolist() == [270.0] * 5
    assert w.loc[D(2026, 3, 3)].load == 270 and w.loc[D(2026, 3, 4)].load == 0


def test_sparse_hr_falls_back_to_band_zones():
    sc = Scenario(first_wake=D(2026, 3, 1), nights=6, hr_cadence_s=600, workouts=every_other_day(D(2026, 3, 1), 6))
    _, wl = daily(sc)
    assert (wl["method"] == "device_zones").all() and wl["load"].eq(270).all()


def test_no_dense_hr_and_no_zones_is_unknown_not_guessed():
    def drop_zones(t):
        for z in range(1, 6):
            t["workouts"][f"zone{z}_s"] = np.nan
    sc = Scenario(first_wake=D(2026, 3, 1), nights=6, hr_cadence_s=600, workouts=every_other_day(D(2026, 3, 1), 6))
    w, wl = daily(sc, drop_zones)
    assert (wl["method"] == "low_resolution").all()
    r = w.loc[D(2026, 3, 3)]
    assert r.load_status == "insufficient_data" and pd.isna(r.load) and pd.isna(r.strain)


def test_workout_free_day_is_zero_only_when_worn():
    sc = Scenario(first_wake=D(2026, 3, 1), nights=6,
                  non_wear=[(dt.datetime(2026, 3, 4, 9), dt.datetime(2026, 3, 4, 20))])
    w, _ = daily(sc)
    assert w.loc[D(2026, 3, 3)].load == 0 and w.loc[D(2026, 3, 3)].load_status == "ok"
    assert w.loc[D(2026, 3, 4)].load_status == "insufficient_data"


def test_hrmax_uses_observed_maxima_and_rejects_artifacts():
    days = np.array(["2026-06-01"], dtype="datetime64[D]")
    ts = pd.to_datetime(["2026-05-01", "2026-05-03", "2026-05-05", "2026-05-07"]).as_unit("s").asi8
    w = pd.DataFrame({"start_ts": ts, "max_hr": [196.0, 197.0, 198.0, 240.0]})
    r = hrmax_by_date(days, w, CFG).iloc[0]
    assert r.hr_max == 197.0 and r.hr_max_basis == "observed"        # median of top 3, 240 rejected
    low = hrmax_by_date(days, w.assign(max_hr=[150.0, 160.0, 170.0, 165.0]), CFG).iloc[0]
    assert low.hr_max_basis == "age" and low.hr_max == pytest.approx(191, abs=0.5)


def test_acwr_steady_spike_warmup_and_missing_days():
    days = np.arange(np.datetime64("2026-01-01"), np.datetime64("2026-03-01"), dtype="datetime64[D]")
    load = np.tile([100.0, 0.0], len(days) // 2 + 1)[:len(days)]
    a = ewma_acwr(days, load, CFG)
    assert a.iloc[20].acwr_status == "warming_up"
    assert a.iloc[-1].acwr_status == "ok" and a.iloc[-1].acwr == pytest.approx(1.0, abs=0.15)

    spike = load.copy()
    spike[-7:] = 200.0
    assert ewma_acwr(days, spike, CFG).iloc[-1].acwr > 1.5

    gaps = load.copy()
    gaps[-3:-1] = np.nan                                  # two unknown days in the last week
    g = ewma_acwr(days, gaps, CFG)
    assert g.iloc[-1].acwr_status == "insufficient_data"
    # an unknown day leaves the averages unchanged (no imputation as 0)
    assert g.iloc[-2].chronic == g.iloc[-4].chronic


def test_unrecorded_session_hint_from_banister():
    def hard_hour(t):
        hr = t["hr_samples"]
        lo = int(pd.Timestamp("2026-03-04 17:00", tz="Europe/Madrid").timestamp())
        hr.loc[hr["ts"].between(lo, lo + 3600), "bpm"] = 165
    # with session detection off, only the Banister hint is left to point at the session
    no_detect = dataclasses.replace(CFG, strain=dataclasses.replace(CFG.strain, detect_sessions=False))
    w, _ = daily(Scenario(first_wake=D(2026, 3, 1), nights=8), hard_hour, no_detect)
    assert w.loc[D(2026, 3, 4)].unrecorded_hint and w.loc[D(2026, 3, 4)].load == 0
    assert not w.loc[D(2026, 3, 5)].unrecorded_hint and w.loc[D(2026, 3, 5)].banister == 0


def test_hrmax_ignores_single_sample_spikes():
    # three hard sessions at 200 bpm sustained would raise HRmax; one-sample spikes must not
    def spikes(t):
        hr = t["hr_samples"]
        for w in t["workouts"].itertuples():
            hr.loc[hr["ts"].between(w.start_ts + 600, w.start_ts + 600), "bpm"] = 215
    w, _ = daily(Scenario(first_wake=D(2026, 3, 1), nights=10, workouts=every_other_day(D(2026, 3, 1), 10, bpm=170)),
                 spikes)
    assert (w["hr_max_basis"] == "age").all()


def test_rest_day_zero_is_marked_unverified_when_hr_is_sparse():
    w, _ = daily(Scenario(first_wake=D(2026, 3, 1), nights=6, hr_cadence_s=600))
    r = w.loc[D(2026, 3, 3)]
    assert r.load == 0 and r.load_status == "ok" and "too sparse" in r.load_reason
    dense, _ = daily(Scenario(first_wake=D(2026, 3, 1), nights=6))
    assert pd.isna(dense.loc[D(2026, 3, 3)].load_reason)


def test_unrecorded_session_is_detected_once_and_walks_are_not():
    def edits(t):
        hr = t["hr_samples"]
        lo = int(pd.Timestamp("2026-03-05 17:00", tz="Europe/Madrid").timestamp())
        hr.loc[hr["ts"].between(lo, lo + 3600 - 1), "bpm"] = 165          # unrecorded hour, zone 4
        lo = int(pd.Timestamp("2026-03-06 10:00", tz="Europe/Madrid").timestamp())
        hr.loc[hr["ts"].between(lo, lo + 3 * 3600), "bpm"] = 105          # long walk, 55 % HRmax
    w, wl = daily(Scenario(first_wake=D(2026, 3, 1), nights=8,
                           workouts={D(2026, 3, 3): (dt.time(18), 90, 150)}), edits)
    assert w.loc[D(2026, 3, 5)].load == 240 and w.loc[D(2026, 3, 5)].load_method == "detected"
    assert w.loc[D(2026, 3, 3)].load == 270 and w.loc[D(2026, 3, 3)].n_workouts == 1   # not double counted
    assert w.loc[D(2026, 3, 6)].load == 0


def test_detector_recall_on_recorded_workouts():
    from miaband.pipeline import detector_recall
    b, _ = generate(Scenario(first_wake=D(2026, 3, 1), nights=10, workouts=every_other_day(D(2026, 3, 1), 10)))
    assert detector_recall(b.tables, CFG) == {"n": 5, "found": 5}
