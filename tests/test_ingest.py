"""Store idempotency and the Mi Fitness adapter (round trip through the real CSV layout)."""
import csv
import datetime as dt
import json

import pandas as pd
import pytest

from miaband import store
from miaband.config import Config
from miaband.pipeline import compute_nightly, load_tables
from miaband.sources import mifitness as mf
from miaband.sources.synthetic import Scenario, generate

NOW = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)


def counts(con):
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("hr_samples", "sleep_sessions", "sleep_segments", "workouts")}


def test_reingest_same_and_overlapping_batches_is_idempotent(tmp_path):
    con = store.connect(tmp_path / "s.sqlite")
    full, _ = generate(Scenario(first_wake=dt.date(2026, 5, 1), nights=20))
    part, _ = generate(Scenario(first_wake=dt.date(2026, 5, 10), nights=5))
    store.write_batch(con, full)
    before = counts(con)
    store.write_batch(con, full)
    store.write_batch(con, part)          # overlapping later export
    assert counts(con) == before


def test_resegmented_night_replaces_old_segments(tmp_path):
    con = store.connect(tmp_path / "s.sqlite")
    batch, _ = generate(Scenario(first_wake=dt.date(2026, 5, 1), nights=3))
    store.write_batch(con, batch)
    # the device re-analyses night 2: one segment, different boundaries
    s = batch.tables["sleep_sessions"].iloc[[1]]
    seg = pd.DataFrame([dict(source="synthetic", device_id=s.device_id.iloc[0],
                             session_start_ts=int(s.start_ts.iloc[0]), start_ts=int(s.start_ts.iloc[0]) + 60,
                             end_ts=int(s.end_ts.iloc[0]), stage="light")])
    redo = type(batch)(source="synthetic", tables={"sleep_sessions": s, "sleep_segments": seg})
    store.write_batch(con, redo)
    got = store.read_table(con, "sleep_segments", "WHERE session_start_ts = ?", (int(s.start_ts.iloc[0]),))
    assert len(got) == 1 and got.start_ts.iloc[0] == int(s.start_ts.iloc[0]) + 60


def test_mifitness_round_trip_matches_canonical(tmp_path):
    sc = Scenario(first_wake=dt.date(2026, 3, 1), nights=40, awake_bouts={dt.date(2026, 3, 5): 20},
                  workouts={dt.date(2026, 3, 10): (dt.time(18), 90, 150)})
    batch, _ = generate(sc)
    mf.write_export(batch, tmp_path)
    got = mf.read_export(tmp_path, now=NOW)
    for t in ("hr_samples", "sleep_sessions", "sleep_segments", "workouts"):
        assert len(got.get(t)) == len(batch.get(t)), t
    assert any("stage code 2 (deep)" in n and "100.0%" in n for n in got.notes)
    w_src = compute_nightly(batch.tables, Config())
    w_mf = compute_nightly(got.tables, Config())
    pd.testing.assert_series_equal(w_src["tst_min"], w_mf["tst_min"])
    pd.testing.assert_series_equal(w_src["rhr"], w_mf["rhr"])


def _write_main(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Uid", "Sid", "Key", "Time", "Value", "UpdateTime"])
        w.writerows(rows)


def test_mifitness_drops_bad_dates_and_rejects_unknown_stage(tmp_path):
    ok_sleep = {"bedtime": 1790802720, "wake_up_time": 1790832180, "timezone": 8,
                "items": [{"start_time": 1790802720, "end_time": 1790832180, "state": 3}]}
    _write_main(tmp_path / "x_1_MiFitness_hlth_center_fitness_data.csv", [
        ["1", "dev", "heart_rate", 978220800, json.dumps({"time": 978220800, "bpm": 60}), 0],   # 2000-12-31
        ["1", "dev", "heart_rate", 1790802780, json.dumps({"time": 1790802780, "bpm": 55}), 0],
        ["1", "dev", "body_momentum", 1790802780, json.dumps({"time": 1790802780, "body_momentum": 0}), 0],
        ["1", "dev", "sleep", 1790832180, json.dumps(ok_sleep), 0],
    ])
    b = mf.read_export(tmp_path, now=NOW)
    assert len(b.get("hr_samples")) == 1
    assert b.get("sleep_sessions").tz_offset_min.iloc[0] == 120
    assert any("impossible date" in n for n in b.notes)

    bad = dict(ok_sleep, items=[{"start_time": 1790802720, "end_time": 1790832180, "state": 9}])
    _write_main(tmp_path / "x_1_MiFitness_hlth_center_fitness_data.csv",
                [["1", "dev", "sleep", 1790832180, json.dumps(bad), 0]])
    with pytest.raises(mf.ImportError_, match="unknown sleep stage codes"):
        mf.read_export(tmp_path, now=NOW)


def test_dst_naive_device_detected_and_corrected():
    # a device that writes +60 all year round across three summers (old Mi Fit behaviour)
    starts = pd.date_range("2019-01-01", "2021-12-31", freq="D", tz="UTC").as_unit("s")
    ts = starts.asi8 + 22 * 3600          # 22:00 UTC each day, in seconds
    sessions = pd.DataFrame({"device_id": "old", "start_ts": ts, "tz_offset_min": 60})
    aware = sessions.assign(device_id="new", tz_offset_min=60 + 60 * mf.in_eu_summer_time(ts))
    verdict = mf.detect_dst_naive(pd.concat([sessions, aware]))
    assert verdict["old"][0] is True and verdict["new"][0] is False

    tables = {"sleep_sessions": sessions.copy(), "workouts": sessions.iloc[:0].copy()}
    mf.fix_dst_naive(tables, notes := [])
    fixed = tables["sleep_sessions"]
    july = fixed[pd.to_datetime(fixed.start_ts, unit="s").dt.month == 7]
    jan = fixed[pd.to_datetime(fixed.start_ts, unit="s").dt.month == 1]
    assert (july.tz_offset_min == 120).all() and (jan.tz_offset_min == 60).all()


def test_cli_import_compute_show(tmp_path, capsys):
    from miaband.cli import main
    batch, _ = generate(Scenario(first_wake=dt.date(2026, 9, 1), nights=30))
    mf.write_export(batch, tmp_path / "export")
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'store = "{(tmp_path / "s.sqlite").as_posix()}"\n')
    assert main(["--config", str(cfg), "import-mifitness", str(tmp_path / "export")]) == 0
    assert main(["--config", str(cfg), "import-mifitness", str(tmp_path / "export")]) == 0   # idempotent
    assert main(["--config", str(cfg), "compute"]) == 0
    assert main(["--config", str(cfg), "show"]) == 0
    assert main(["--config", str(cfg), "sanity", "--days", "5"]) == 0
    out = capsys.readouterr().out
    assert "imported before" in out and "7h30" in out and "TST vs band: mean Δ +0.0" in out
