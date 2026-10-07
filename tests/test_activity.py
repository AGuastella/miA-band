"""Daily steps: device overlap, worn days with no rows, unworn days."""
import datetime as dt

import pandas as pd

from miaband.config import Config
from miaband.pipeline import compute_daily
from miaband.sources.synthetic import Scenario, generate

D = dt.date


def test_daily_steps_rules():
    sc = Scenario(first_wake=D(2026, 5, 1), nights=8, steps_by_date={D(2026, 5, 3): 15000, D(2026, 5, 4): 0},
                  non_wear=[(dt.datetime(2026, 5, 6, 7), dt.datetime(2026, 5, 6, 23))])
    b, _ = generate(sc)
    # a second device (e.g. a phone) counted part of the same walk on 5/3: must not be added
    extra = b.tables["step_samples"].head(30).assign(device_id="phone")
    b.tables["step_samples"] = pd.concat([b.tables["step_samples"], extra], ignore_index=True)
    w, _ = compute_daily(b.tables, Config())
    w = w.set_index(w["date"].dt.date)
    assert w.loc[D(2026, 5, 2)].steps == 8000
    assert w.loc[D(2026, 5, 3)].steps == 15000                   # max over devices, not the sum
    assert w.loc[D(2026, 5, 4)].steps == 0 and w.loc[D(2026, 5, 4)].steps_status == "ok"    # worn, no walking
    r = w.loc[D(2026, 5, 6)]
    assert pd.isna(r.steps) and r.steps_status == "insufficient_data"                    # band off all day
