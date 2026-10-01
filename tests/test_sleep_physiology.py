"""Feature tests against planted synthetic patterns with known expected outputs."""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from miaband.config import Config
from miaband.features.sleep import circular_sd_min
from miaband.pipeline import compute_nightly, to_long
from miaband.sources.synthetic import Scenario, generate

D = dt.date
CFG = Config()


def run(sc: Scenario, cfg: Config = CFG) -> pd.DataFrame:
    batch, _ = generate(sc)
    w = compute_nightly(batch.tables, cfg)
    return w.set_index(w["date"].dt.date)


@pytest.fixture(scope="module")
def planted():
    sc = Scenario(
        first_wake=D(2026, 3, 1), nights=42,
        short_nights={D(2026, 3, 20): 5.0},
        awake_bouts={D(2026, 3, 21): 30},
        naps={D(2026, 3, 22): (dt.time(15, 0), 40)},
        missing_nights={D(2026, 3, 25)},
        rhr_delta={D(2026, 4, 5): 8},
    )
    return run(sc)


def test_regular_nights_exact(planted):
    r = planted.loc[D(2026, 3, 10)]
    assert r.tst_min == 450 and r.spt_min == 450 and r.waso_min == 0
    assert r.onset_clock == 23 * 60 + 30 and r.wake_clock == 7 * 60
    assert (r.deep_min, r.light_min, r.rem_min) == (125, 225, 100)   # 5 full cycles: 45/25/20
    assert r.rhr == pytest.approx(52.0)


def test_short_night_and_awake_bout(planted):
    assert planted.loc[D(2026, 3, 20)].tst_min == 300
    r = planted.loc[D(2026, 3, 21)]
    assert r.spt_min == 450 and r.tst_min == 420 and r.waso_min == 30
    assert r.sme == pytest.approx(420 / 450)


def test_nap_kept_out_of_main_sleep(planted):
    r = planted.loc[D(2026, 3, 22)]
    assert r.nap_min == 40 and r.tst_min == 450


def test_missing_night_is_explicit_not_imputed(planted):
    r = planted.loc[D(2026, 3, 25)]
    assert pd.isna(r.episode) and pd.isna(r.tst_min) and pd.isna(r.rhr)
    long = to_long(planted.reset_index(drop=True))
    row = long[(long.date == "2026-03-25") & (long.metric == "sleep.tst_min")].iloc[0]
    assert row.status == "insufficient_data" and pd.isna(row.value)


def test_baseline_warmup_then_flag(planted):
    # 14 valid nights are needed before the first z-score
    assert planted.loc[D(2026, 3, 14)].rhr_base_status == "warming_up"
    assert planted.loc[D(2026, 3, 15)].rhr_base_status == "ok"
    ill = planted.loc[D(2026, 4, 5)]
    assert ill.rhr == pytest.approx(60.0)
    assert ill.rhr_z == pytest.approx(8.0)          # spread floor 1 bpm, centre 52
    assert ill.rhr_flag
    assert not planted.loc[D(2026, 4, 6)].rhr_flag  # the sick night doesn't move the median


def test_short_night_flagged_against_tst_baseline(planted):
    r = planted.loc[D(2026, 3, 20)]
    assert r.tst_z == pytest.approx((300 - 450) / 20) and r.tst_flag


def test_dst_night_wall_clock_and_no_tz_change(planted):
    # 2026-03-29: clocks go forward during the night; bedtime 23:30 CET, 7.5 h later is 08:00 CEST
    r = planted.loc[D(2026, 3, 29)]
    assert r.onset_clock == 23 * 60 + 30 and r.wake_clock == 8 * 60 and r.spt_min == 450
    assert not planted["tz_change"].fillna(False).any()


def test_autumn_dst_and_wake_date():
    w = run(Scenario(first_wake=D(2026, 10, 20), nights=14))
    r = w.loc[D(2026, 10, 25)]           # clocks go back: 7.5 h after 23:30 CEST is 06:00 CET
    assert r.wake_clock == 6 * 60 and r.spt_min == 450
    assert list(w.index) == [D(2026, 10, 20) + dt.timedelta(days=i) for i in range(14)]


def test_after_midnight_bedtime_belongs_to_same_wake_date():
    w = run(Scenario(first_wake=D(2026, 5, 1), nights=10, bedtime_shift_min={D(2026, 5, 5): 90}))
    r = w.loc[D(2026, 5, 5)]
    assert r.onset_clock == 60 and r.wake_clock == 8 * 60 + 30


def test_rhr_sparse_cadence_widens_window():
    w = run(Scenario(first_wake=D(2026, 5, 1), nights=5, hr_cadence_s=600))
    r = w.iloc[2]
    assert r.rhr_window_min == 30 and r.phys_status == "ok"
    assert r.rhr == pytest.approx(52.0, abs=1.5)


def test_rhr_insufficient_when_band_off_half_the_night():
    sc = Scenario(first_wake=D(2026, 5, 1), nights=5,
                  non_wear=[(dt.datetime(2026, 5, 3, 1), dt.datetime(2026, 5, 3, 6))])
    r = run(sc).loc[D(2026, 5, 3)]
    assert r.phys_status == "insufficient_data" and pd.isna(r.rhr)
    assert r.hr_coverage < 0.8


def test_rhr_robust_to_single_spike_and_noise():
    sc = Scenario(first_wake=D(2026, 5, 1), nights=5, noise_sd=2.0, seed=7)
    batch, _ = generate(sc)
    hr = batch.tables["hr_samples"]
    hr.loc[hr.index[len(hr) // 2], "bpm"] = 30      # one optical dropout
    w = compute_nightly(batch.tables, CFG)
    assert w["rhr"].dropna().between(50, 54).all()


def test_regularity_regular_vs_jittered():
    regular = run(Scenario(first_wake=D(2026, 5, 1), nights=21))
    jitter = run(Scenario(first_wake=D(2026, 5, 1), nights=21, bedtime_jitter_min=60, seed=3))
    assert regular.iloc[-2].sri == pytest.approx(100.0)
    assert regular.iloc[-2].onset_csd == pytest.approx(0.0, abs=0.1)
    assert jitter.iloc[-2].sri < 92     # ~2 x E|N(0, 60 min)| mismatched per day pair
    assert 30 < jitter.iloc[-2].onset_csd < 120
    assert regular.iloc[3].reg_status == "insufficient_data"   # < 5 nights in window


def test_circular_sd_wraps_midnight():
    assert circular_sd_min([23 * 60 + 50, 10]) == pytest.approx(10.0, abs=0.1)


def test_travel_offset_is_a_tz_change_and_excluded_from_regularity():
    trip = {D(2026, 5, 10) + dt.timedelta(days=i): "Asia/Shanghai" for i in range(5)}
    w = run(Scenario(first_wake=D(2026, 5, 1), nights=21, tz_by_date=trip))
    assert w.loc[D(2026, 5, 10)].tz_change and w.loc[D(2026, 5, 15)].tz_change
    # local clock in Shanghai is still 23:30 -> 07:00
    assert w.loc[D(2026, 5, 12)].onset_clock == 23 * 60 + 30
    assert w.loc[D(2026, 5, 10)].reg_n < w.loc[D(2026, 5, 9)].reg_n


def test_era_change_restarts_baseline():
    a, _ = generate(Scenario(first_wake=D(2026, 3, 1), nights=20, device_id="old"))
    b, _ = generate(Scenario(first_wake=D(2026, 3, 21), nights=5, device_id="new", nadir_hr=58))
    tables = {k: pd.concat([a.tables[k], b.tables[k]], ignore_index=True) for k in a.tables}
    w = compute_nightly(tables, CFG).set_index("date")
    first_new = w.loc[pd.Timestamp("2026-03-21")]
    assert first_new.rhr == pytest.approx(58.0)
    assert first_new.rhr_base_status == "warming_up" and not first_new.rhr_flag


def test_sri_survives_one_unworn_night_in_window():
    w = run(Scenario(first_wake=D(2026, 5, 1), nights=21, missing_nights={D(2026, 5, 15)}))
    assert w.loc[D(2026, 5, 18)].sri == pytest.approx(100.0)   # pairs with 5/15 still count (~69 % known)
