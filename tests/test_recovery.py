"""Recovery score decomposition and readiness rules."""
import dataclasses
import datetime as dt

import pandas as pd
import pytest

from miaband.config import Config, Readiness
from miaband.features.recovery import readiness
from miaband.pipeline import compute_daily
from miaband.sources.synthetic import Scenario, generate

D = dt.date


@pytest.fixture(scope="module")
def wide():
    sc = Scenario(first_wake=D(2026, 3, 1), nights=40,
                  rhr_delta={D(2026, 4, 2): 8, D(2026, 4, 5): 3},
                  short_nights={D(2026, 4, 5): 5.0, D(2026, 4, 8): 6.5},
                  non_wear=[(dt.datetime(2026, 4, 6, 1), dt.datetime(2026, 4, 6, 6))])
    w, _ = compute_daily(generate(sc)[0].tables, Config())
    return w.set_index(w["date"].dt.date)


def test_neutral_night_scores_50(wide):
    r = wide.loc[D(2026, 3, 25)]
    assert r.recovery_status == "ok" and r.recovery == pytest.approx(50.0)


def test_points_decompose_score_exactly(wide):
    ok = wide[wide["recovery_status"] == "ok"]
    total = 50 + ok["recovery_pts_rhr"] + ok["recovery_pts_sleep"]
    assert (total - ok["recovery"]).abs().max() < 1e-9


def test_high_rhr_lowers_score_and_recommends_rest(wide):
    r = wide.loc[D(2026, 4, 2)]
    assert r.recovery < 5 and r.recovery_band == "low" and r.recovery_pts_sleep == 0
    assert r.readiness == "Recovery day recommended"


def test_illness_rule_needs_high_rhr_and_short_night(wide):
    assert wide.loc[D(2026, 4, 5)].readiness.startswith("Resting HR well above baseline")


def test_short_night_alone_is_a_sleep_penalty(wide):
    r = wide.loc[D(2026, 4, 8)]
    assert r.recovery_z_sleep == pytest.approx(-3.0)        # (390 - 450) / 20, clipped
    assert r.recovery_pts_sleep < 0 and r.recovery_pts_rhr == pytest.approx(0, abs=5)


def test_warmup_and_missing_component_give_no_score(wide):
    early = wide.loc[D(2026, 3, 5)]
    assert early.recovery_status == "warming_up" and pd.isna(early.recovery)
    assert early.readiness == "Insufficient data for a recommendation"
    off = wide.loc[D(2026, 4, 6)]                           # band off half the night: no RHR
    assert off.recovery_status == "insufficient_data" and pd.isna(off.recovery)


def test_custom_rules_and_validation(wide):
    rules = ({"when": [{"field": "sleep.tst_min", "op": "<", "value": 400}], "say": "nap today"},
             {"when": [], "say": "ok"})
    cfg = dataclasses.replace(Config(), readiness=Readiness(rules=rules))
    out = readiness(wide.reset_index(drop=True), cfg).set_index("date")
    assert out.loc[pd.Timestamp("2026-04-08"), "readiness"] == "nap today"
    assert out.loc[pd.Timestamp("2026-03-25"), "readiness"] == "ok"
    bad = dataclasses.replace(Config(), readiness=Readiness(rules=({"when": [{"field": "hrv", "op": ">", "value": 1}], "say": "x"},)))
    with pytest.raises(ValueError, match="bad term"):
        readiness(wide.reset_index(drop=True), bad)
