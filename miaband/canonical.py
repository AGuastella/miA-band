"""Canonical internal schema (docs/SPEC.md §2).

Every source adapter produces a `CanonicalBatch`: one DataFrame per table, columns exactly as
declared here. All instants are UTC epoch seconds (milliseconds for RR). Offsets are minutes
east of UTC as recorded by the device for that record.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

STAGES = ("awake", "light", "deep", "rem", "asleep")      # 'asleep' = asleep, stage not given
HR_CONTEXTS = ("background", "workout", "spot", "unknown")

# strategy: "point" = upsert on key; "interval" = replace-by-window on (source, start/end)
TABLES: dict[str, dict] = {
    "hr_samples": dict(
        columns={"source": "TEXT", "device_id": "TEXT", "ts": "INTEGER", "bpm": "INTEGER",
                 "context": "TEXT"},
        key=("source", "device_id", "ts"), strategy="point"),
    "step_samples": dict(
        columns={"source": "TEXT", "device_id": "TEXT", "ts": "INTEGER", "steps": "INTEGER"},
        key=("source", "device_id", "ts"), strategy="point"),
    "sleep_sessions": dict(
        columns={"source": "TEXT", "device_id": "TEXT", "start_ts": "INTEGER", "end_ts": "INTEGER",
                 "is_nap": "INTEGER",            # 1 = device says nap, NULL = let features decide
                 "in_bed_start_ts": "INTEGER", "in_bed_end_ts": "INTEGER",
                 "tz_offset_min": "INTEGER",
                 # device-reported values, kept for sanity checks only
                 "dev_duration_min": "REAL", "dev_deep_min": "REAL", "dev_light_min": "REAL",
                 "dev_rem_min": "REAL", "dev_awake_min": "REAL",
                 "dev_min_hr": "REAL", "dev_avg_hr": "REAL"},
        key=("source", "device_id", "start_ts"), strategy="interval"),
    "sleep_segments": dict(
        columns={"source": "TEXT", "device_id": "TEXT", "session_start_ts": "INTEGER",
                 "start_ts": "INTEGER", "end_ts": "INTEGER", "stage": "TEXT"},
        key=("source", "device_id", "start_ts", "session_start_ts"), strategy="interval"),
    "workouts": dict(
        columns={"source": "TEXT", "device_id": "TEXT", "start_ts": "INTEGER", "end_ts": "INTEGER",
                 "sport": "TEXT", "tz_offset_min": "INTEGER",
                 "avg_hr": "REAL", "max_hr": "REAL", "min_hr": "REAL",
                 "zone1_s": "REAL", "zone2_s": "REAL", "zone3_s": "REAL", "zone4_s": "REAL",
                 "zone5_s": "REAL", "dev_train_load": "REAL"},
        key=("source", "device_id", "start_ts"), strategy="interval"),
    "rr_intervals": dict(
        columns={"source": "TEXT", "device_id": "TEXT", "ts_ms": "INTEGER", "rr_ms": "INTEGER"},
        key=("source", "device_id", "ts_ms"), strategy="point"),
    "device_daily": dict(
        columns={"source": "TEXT", "device_id": "TEXT", "date": "TEXT", "metric": "TEXT",
                 "value": "REAL"},
        key=("source", "device_id", "date", "metric"), strategy="point"),
}


def empty(table: str) -> pd.DataFrame:
    cols = TABLES[table]["columns"]
    return pd.DataFrame({c: pd.Series(dtype="object" if t == "TEXT" else "float64")
                         for c, t in cols.items()})


@dataclass
class CanonicalBatch:
    source: str
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)     # adapter diagnostics for the user

    def get(self, table: str) -> pd.DataFrame:
        return self.tables.get(table, empty(table))

    def validate(self) -> None:
        """Structural checks shared by every adapter. Raises ValueError on the first problem."""
        for name, df in self.tables.items():
            if name not in TABLES:
                raise ValueError(f"unknown canonical table {name!r}")
            spec = TABLES[name]
            missing = set(spec["columns"]) - set(df.columns)
            extra = set(df.columns) - set(spec["columns"])
            if missing or extra:
                raise ValueError(f"{name}: missing columns {sorted(missing)}, extra {sorted(extra)}")
            if df.empty:
                continue
            if df[list(spec["key"])].isna().any().any():
                raise ValueError(f"{name}: NULL in key columns {spec['key']}")
            if (df["source"] != self.source).any():
                raise ValueError(f"{name}: rows with a source other than {self.source!r}")
            if df.duplicated(list(spec["key"])).any():
                raise ValueError(f"{name}: duplicate keys {spec['key']}")
            if "end_ts" in df and (df["end_ts"] <= df["start_ts"]).any():
                raise ValueError(f"{name}: end_ts <= start_ts")
            if name == "sleep_segments" and not df["stage"].isin(STAGES).all():
                raise ValueError(f"sleep_segments: unknown stage(s) {set(df['stage']) - set(STAGES)}")
            if name == "hr_samples" and not df["context"].isin(HR_CONTEXTS).all():
                raise ValueError("hr_samples: unknown context")
