"""Smoke test for the Step 0 inspector against a tiny synthetic Gadgetbridge-like DB."""
import importlib.util
import sqlite3
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "inspect_gadgetbridge", Path(__file__).parents[1] / "scripts" / "inspect_gadgetbridge.py")
ig = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ig)


def make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE DEVICE (_id INTEGER PRIMARY KEY, NAME TEXT, IDENTIFIER TEXT);
        CREATE TABLE XIAOMI_ACTIVITY_SAMPLE (TIMESTAMP INTEGER, DEVICE_ID INTEGER,
            RAW_KIND INTEGER, HEART_RATE INTEGER, PRIMARY KEY (TIMESTAMP, DEVICE_ID));
        CREATE TABLE XIAOMI_SLEEP_STAGE_SAMPLE (TIMESTAMP INTEGER, DEVICE_ID INTEGER, STAGE INTEGER);
        CREATE TABLE SOME_HRV_TABLE (TIMESTAMP INTEGER, RR_INTERVALS BLOB);
    """)
    con.execute("INSERT INTO DEVICE VALUES (1, 'Band', 'AA:BB:CC:DD:EE:FF')")
    t0 = 1_743_289_200  # 2025-03-30 01:00 Europe/Rome, straddles the spring-forward DST gap
    con.executemany("INSERT INTO XIAOMI_ACTIVITY_SAMPLE VALUES (?, 1, ?, ?)",
                    [(t0 + 60 * i, i % 3, 0 if i % 10 == 0 else 55 + i % 20) for i in range(600)])
    con.executemany("INSERT INTO XIAOMI_SLEEP_STAGE_SAMPLE VALUES (?, 1, ?)",
                    [((t0 + 600 * i) * 1000, 2 + i % 4) for i in range(30)])
    con.execute("INSERT INTO SOME_HRV_TABLE VALUES (?, ?)", (t0, bytes([3, 32, 3, 40])))
    con.commit()
    con.close()


def test_ts_unit():
    assert ig.ts_unit(1_700_000_000) == "s"
    assert ig.ts_unit(1_700_000_000_000) == "ms"
    assert ig.ts_unit(420) is None


def test_end_to_end(tmp_path):
    db = tmp_path / "Gadgetbridge"
    make_db(db)
    out = tmp_path / "out"
    assert ig.main([str(db), "--tz", "Europe/Rome", "--out", str(out)]) == 0

    report = (out / "report.txt").read_text()
    assert "XIAOMI_ACTIVITY_SAMPLE" in report
    assert "values of STAGE: 2:" in report
    assert "median Δ=      60s" in report
    assert "0×60" in report                                   # HR sentinel zeros counted
    assert "+01:00" in report and "+02:00" in report         # DST-aware local rendering
    assert "SOME_HRV_TABLE.RR_INTERVALS" in report           # populated HRV candidate
    assert "AA:BB:CC" not in report                          # MAC redacted
    assert "CREATE TABLE DEVICE" in (out / "schema.sql").read_text()
    assert "AA:BB:CC" not in (out / "samples.txt").read_text()
