"""Smoke test for the Mi Fitness export inspector on a tiny synthetic key/value export."""
import csv
import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "inspect_mifitness", Path(__file__).parents[1] / "scripts" / "inspect_mifitness.py")
im = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(im)

ACCOUNT = "6612345678"


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def make_export(d: Path) -> None:
    t0 = 1_774_738_800  # 2026-03-29 00:00 Europe/Madrid, across the spring-forward gap
    rows = [[ACCOUNT, "blt.new", "heart_rate", t0 + 60 * i,
             json.dumps({"time": t0 + 60 * i, "bpm": 55 + i % 30}), t0] for i in range(300)]
    rows.append([ACCOUNT, "blt.new", "heart_rate", t0, json.dumps({"time": t0, "bpm": 56}), t0])  # dup ts
    rows.append([ACCOUNT, "blt.old", "watch_night_sleep", t0 - 86400 * 400, json.dumps(
        {"bedtime": t0 - 86400 * 400, "items": [{"start_time": t0 - 86400 * 400, "state": 3}]}), t0])
    write_csv(d / f"20261001_{ACCOUNT}_MiFitness_hlth_center_fitness_data.csv",
              ["Uid", "Sid", "Key", "Time", "Value", "UpdateTime"], rows)
    write_csv(d / f"20261001_{ACCOUNT}_MiFitness_hlth_center_sport_track_data.csv",
              ["Uid", "Time", "Latitude", "Longitude"], [[ACCOUNT, t0, "40.4168", "-3.7038"]])


def test_end_to_end(tmp_path):
    exp = tmp_path / "export"
    exp.mkdir()
    make_export(exp)
    out = tmp_path / "out"
    assert im.main([str(exp), "--out", str(out)]) == 0
    report = (out / "report.txt").read_text(encoding="utf-8")
    samples = (out / "samples.txt").read_text(encoding="utf-8")

    assert "detected roles: time=Time, key=Key, value=Value, device=Sid" in report
    assert "--- key 'heart_rate': 301 rows" in report
    assert "duplicate timestamps 1" in report
    assert "median Δ 60s" in report
    assert "items[].state" in report and "values{3:1}" in report
    assert "+01:00" in report and "+02:00" in report          # DST-aware local rendering
    assert "VERDICT: no key, column or JSON path" in report
    for text in (report, samples):
        assert ACCOUNT not in text                              # account id masked everywhere
        assert "40.4168" not in text and "-3.7038" not in text  # GPS never printed
