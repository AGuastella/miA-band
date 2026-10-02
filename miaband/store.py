"""SQLite store: canonical raw tables, ingest log and daily metrics (docs/SPEC.md §2.1).

Point tables are upserted on their natural key (later export wins). Interval tables are
replaced by window: rows of the same source overlapping the batch's time span are deleted and
the batch's rows inserted, in one transaction, so a night the device re-segmented between two
exports leaves no orphans.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from .canonical import TABLES, CanonicalBatch

DAILY_DDL = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT NOT NULL, metric TEXT NOT NULL, value REAL, status TEXT NOT NULL,
    reason TEXT, computed_at TEXT NOT NULL, PRIMARY KEY (date, metric));
CREATE TABLE IF NOT EXISTS ingest_log (
    batch_id TEXT NOT NULL, source TEXT NOT NULL, file_sha256 TEXT, ingested_at TEXT NOT NULL,
    tbl TEXT NOT NULL, rows INTEGER NOT NULL, min_ts INTEGER, max_ts INTEGER, notes TEXT);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    init_schema(con)
    return con


def init_schema(con: sqlite3.Connection) -> None:
    for name, spec in TABLES.items():
        cols = ", ".join(f"{c} {t}" for c, t in spec["columns"].items())
        con.execute(f"CREATE TABLE IF NOT EXISTS {name} ({cols}, PRIMARY KEY ({', '.join(spec['key'])}))")
        if spec["strategy"] == "interval":
            con.execute(f"CREATE INDEX IF NOT EXISTS {name}_window ON {name} (source, start_ts, end_ts)")
    con.executescript(DAILY_DDL)
    con.commit()


def _py(v):
    """numpy/pandas scalars -> plain Python for sqlite3."""
    if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA:
        return None
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return None if np.isnan(v) else float(v)
    return v


def _rows(df: pd.DataFrame, cols: list[str]):
    for rec in df[cols].itertuples(index=False, name=None):
        yield tuple(_py(v) for v in rec)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_batch(con: sqlite3.Connection, batch: CanonicalBatch, file_hash: str | None = None) -> dict:
    """Ingest a validated batch. Returns {table: rows written}. Idempotent."""
    batch.validate()
    batch_id = uuid.uuid4().hex
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    written = {}
    with con:  # one transaction for the whole batch
        for name, df in batch.tables.items():
            spec = TABLES[name]
            cols = list(spec["columns"])
            if df.empty:
                continue
            if spec["strategy"] == "interval":
                lo, hi = int(df["start_ts"].min()), int(df["end_ts"].max())
                con.execute(f"DELETE FROM {name} WHERE source = ? AND start_ts < ? AND end_ts > ?",
                            (batch.source, hi, lo))
                con.executemany(f"INSERT INTO {name} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                _rows(df, cols))
                min_ts, max_ts = lo, hi
            else:
                updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in spec["key"])
                sql = (f"INSERT INTO {name} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                       f"ON CONFLICT ({', '.join(spec['key'])}) DO "
                       + (f"UPDATE SET {updates}" if updates else "NOTHING"))
                con.executemany(sql, _rows(df, cols))
                tcol = "ts" if "ts" in df else ("ts_ms" if "ts_ms" in df else None)
                min_ts = int(df[tcol].min()) if tcol else None
                max_ts = int(df[tcol].max()) if tcol else None
            written[name] = len(df)
            con.execute("INSERT INTO ingest_log VALUES (?,?,?,?,?,?,?,?,?)",
                        (batch_id, batch.source, file_hash, now, name, len(df), min_ts, max_ts,
                         "\n".join(batch.notes) or None))
    return written


def already_ingested(con: sqlite3.Connection, file_hash: str) -> bool:
    return con.execute("SELECT 1 FROM ingest_log WHERE file_sha256 = ? LIMIT 1", (file_hash,)).fetchone() is not None


def read_table(con: sqlite3.Connection, name: str, where: str = "", params: tuple = ()) -> pd.DataFrame:
    df = pd.read_sql_query(f"SELECT * FROM {name} {where}", con, params=params)
    return df


def write_daily(con: sqlite3.Connection, daily: pd.DataFrame) -> int:
    """Replace all daily metrics (features are a full recompute; docs/SPEC.md §1)."""
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    cols = ["date", "metric", "value", "status", "reason"]
    with con:
        con.execute("DELETE FROM daily_metrics")
        con.executemany("INSERT INTO daily_metrics VALUES (?,?,?,?,?,?)",
                        (row + (now,) for row in _rows(daily, cols)))
    return len(daily)
