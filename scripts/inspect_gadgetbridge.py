#!/usr/bin/env python3
"""Step 0 — inspect a Gadgetbridge SQLite export before designing anything on top of it.

Stdlib only (sqlite3 + zoneinfo), read-only, no network. Produces three files in --out:

  report.txt   tables, row counts, time ranges, per-column null/zero rates, code-column
               value distributions, sampling cadence, HR coverage/non-wear, RR/HRV verdict
  schema.sql   equivalent of sqlite3 `.schema`
  samples.txt  first/last rows of every non-empty table (timestamps also shown in local time)

Usage:
  python scripts/inspect_gadgetbridge.py path/to/Gadgetbridge --tz Europe/Rome [--out inspect_out]

Redaction (on by default, disable with --no-redact): MAC-address-like strings and the
USER.NAME column are masked, so the outputs can be pasted into a conversation.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------------------
# Heuristics. Deliberately name-based and loose: the point is to surface candidates for a
# human to confirm, not to decide the schema.
# ---------------------------------------------------------------------------------------
TS_NAME = re.compile(r"((^|_)(TIMESTAMP|TIME|DATE|START|END|DAY)(_|$)|BIRTHDAY)", re.I)
CODE_NAME = re.compile(r"(KIND|STAGE|TYPE|STATE|MODE|SOURCE|ACTIVITY|STATUS|^IS_|_FLAG|CATEGORY)", re.I)
HR_NAME = re.compile(r"(HEART_?RATE|(^|_)HR($|_)|BPM)", re.I)
HRV_NAME = re.compile(r"(HRV|RMSSD|SDNN|(^|_)RR($|_)|RRI|RR_?INTERVAL|(^|_)IBI($|_)|BEAT|INTERVAL)", re.I)
SLEEP_NAME = re.compile(r"SLEEP", re.I)
MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")

MAX_CODE_DISTINCT = 64        # print value counts for code-like columns up to this many values
MAX_SMALL_INT_DISTINCT = 16   # ...and for any integer column with this few distinct values
CADENCE_ROWS = 200_000        # rows (most recent) used to estimate sampling cadence
HR_VALID = (30, 220)          # physiological plausibility window for a per-sample HR value


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def connect_ro(path: Path) -> sqlite3.Connection:
    # immutable=1: the export is a standalone snapshot; never create -wal/-shm next to it.
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


def ts_unit(v: float | None) -> str | None:
    """Guess the unit of an epoch value. Gadgetbridge mixes seconds (older activity tables)
    and milliseconds (newer tables); anything below ~1973 in seconds is not an epoch."""
    if v is None:
        return None
    v = abs(v)
    if v > 1e17:
        return "ns"
    if v > 1e14:
        return "us"
    if v > 1e11:
        return "ms"
    if v > 1e8:
        return "s"
    return None


def to_dt(v: float, unit: str, tz: ZoneInfo) -> dt.datetime:
    div = {"s": 1, "ms": 1e3, "us": 1e6, "ns": 1e9}[unit]
    return dt.datetime.fromtimestamp(v / div, tz=dt.timezone.utc).astimezone(tz)


def fmt_dt(v: float, unit: str, tz: ZoneInfo) -> str:
    return to_dt(v, unit, tz).isoformat(timespec="seconds")


def redact(s: str, enabled: bool) -> str:
    return MAC_RE.sub("XX:XX:XX:XX:XX:XX", s) if enabled else s


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, s: str = "") -> None:
        self.lines.append(s)

    def h(self, title: str) -> None:
        self.lines += ["", "=" * 88, title, "=" * 88]

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


# ---------------------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------------------
def tables(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name != 'android_metadata' ORDER BY name")]


def columns(con: sqlite3.Connection, t: str) -> list[sqlite3.Row]:
    return list(con.execute(f"PRAGMA table_info({q(t)})"))


def dump_schema(con: sqlite3.Connection) -> str:
    rows = con.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, tbl_name, name")
    return "\n".join(r[0].strip() + ";" for r in rows) + "\n"


def column_stats(con: sqlite3.Connection, t: str, cols: list[sqlite3.Row]) -> dict[str, dict]:
    """One pass per table: null count, zero count, distinct count, min, max, avg blob length."""
    exprs = []
    for c in cols:
        n = q(c["name"])
        exprs += [
            f"SUM({n} IS NULL)",
            f"SUM({n} = 0)",
            f"COUNT(DISTINCT {n})",
            f"MIN(CASE WHEN typeof({n}) IN ('integer','real') THEN {n} END)",
            f"MAX(CASE WHEN typeof({n}) IN ('integer','real') THEN {n} END)",
            f"AVG(CASE WHEN typeof({n}) = 'blob' THEN length({n}) END)",
            f"GROUP_CONCAT(DISTINCT typeof({n}))",
        ]
    row = con.execute(f"SELECT {', '.join(exprs)} FROM {q(t)}").fetchone()
    out = {}
    for i, c in enumerate(cols):
        nulls, zeros, distinct, mn, mx, blob_len, types = row[i * 7:(i + 1) * 7]
        out[c["name"]] = dict(nulls=nulls or 0, zeros=zeros or 0, distinct=distinct, min=mn,
                              max=mx, blob_len=blob_len, types=types or "")
    return out


def value_counts(con: sqlite3.Connection, t: str, col: str, limit: int) -> list[tuple]:
    return [tuple(r) for r in con.execute(
        f"SELECT {q(col)}, COUNT(*) FROM {q(t)} GROUP BY 1 ORDER BY 2 DESC LIMIT {limit}")]


def primary_ts_column(cols: list[sqlite3.Row], stats: dict[str, dict]) -> tuple[str, str] | None:
    """The column most likely to be the row's event time: prefer TIMESTAMP, then *TIME/*START."""
    cands = []
    for c in cols:
        name = c["name"]
        unit = ts_unit(stats[name]["max"])
        if unit and TS_NAME.search(name):
            rank = 0 if name.upper() == "TIMESTAMP" else 1 if "START" in name.upper() else 2
            cands.append((rank, name, unit))
    return (sorted(cands)[0][1], sorted(cands)[0][2]) if cands else None


# ---------------------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------------------
def section_overview(rep: Report, con, db: Path, tz: ZoneInfo, counts: dict[str, int]) -> None:
    rep.h("OVERVIEW")
    uv = con.execute("PRAGMA user_version").fetchone()[0]
    rep(f"file:            {db}  ({db.stat().st_size / 1e6:.1f} MB)")
    rep(f"schema version:  PRAGMA user_version = {uv}  (Gadgetbridge DB schema version)")
    rep(f"sqlite library:  {sqlite3.sqlite_version}")
    rep(f"report timezone: {tz.key}")
    rep(f"tables:          {len(counts)} total, {sum(1 for v in counts.values() if v)} non-empty")
    empty = sorted(t for t, n in counts.items() if n == 0)
    rep("")
    rep("Non-empty tables (rows):")
    for t, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if n:
            rep(f"  {t:<48} {n:>12,}")
    rep("")
    rep(f"Empty tables ({len(empty)}): " + ", ".join(empty))


def section_devices(rep: Report, con, counts: dict[str, int], redact_on: bool) -> None:
    rep.h("DEVICES / USER")
    for t in ("DEVICE", "DEVICE_ATTRIBUTES", "USER", "USER_ATTRIBUTES"):
        if not counts.get(t):
            continue
        rep(f"-- {t}")
        for r in con.execute(f"SELECT * FROM {q(t)}"):
            d = dict(r)
            if redact_on and t == "USER" and "NAME" in d:
                d["NAME"] = "<redacted>"
            rep("  " + redact(repr(d), redact_on))


def section_tables(rep: Report, con, tz: ZoneInfo, counts: dict[str, int]) -> dict:
    """Per-table detail. Returns per-table metadata reused by later sections."""
    rep.h("PER-TABLE DETAIL (non-empty tables)")
    meta = {}
    for t in sorted(t for t, n in counts.items() if n):
        n = counts[t]
        cols = columns(con, t)
        stats = column_stats(con, t, cols)
        pts = primary_ts_column(cols, stats)
        meta[t] = dict(cols=cols, stats=stats, ts=pts)

        rep("")
        rep(f"### {t}  ({n:,} rows)")
        if pts:
            col, unit = pts
            s = stats[col]
            rep(f"    time column: {col} [{unit}]  range {fmt_dt(s['min'], unit, tz)} -> "
                f"{fmt_dt(s['max'], unit, tz)}")
        rep(f"    {'column':<34}{'decl type':<12}{'stored':<16}{'null%':>7}{'zero%':>7}"
            f"{'distinct':>10}  min .. max")
        for c in cols:
            s = stats[c["name"]]
            unit = ts_unit(s["max"]) if TS_NAME.search(c["name"]) else None
            if s["blob_len"] is not None:
                rng = f"blob avg {s['blob_len']:.0f} B"
            elif s["min"] is None:
                rng = ""
            elif unit:
                rng = f"{fmt_dt(s['min'], unit, tz)} .. {fmt_dt(s['max'], unit, tz)} [{unit}]"
            else:
                rng = f"{s['min']} .. {s['max']}"
            rep(f"    {c['name']:<34}{(c['type'] or '-'):<12}{s['types']:<16}"
                f"{100 * s['nulls'] / n:>7.1f}{100 * s['zeros'] / n:>7.1f}{s['distinct']:>10}  {rng}")

        # Value distributions of code-like / small-cardinality integer columns.
        for c in cols:
            s = stats[c["name"]]
            is_code = CODE_NAME.search(c["name"]) and s["distinct"] <= MAX_CODE_DISTINCT
            small_int = ("integer" in s["types"] and s["distinct"] <= MAX_SMALL_INT_DISTINCT
                         and not TS_NAME.search(c["name"]) and c["name"] not in ("_id", "ID"))
            if (is_code or small_int) and s["distinct"] > 0:
                vc = value_counts(con, t, c["name"], MAX_CODE_DISTINCT)
                rep(f"    values of {c['name']}: " +
                    ", ".join(f"{v!r}:{k:,}" for v, k in sorted(vc, key=lambda x: (x[0] is None, x[0]))))
    return meta


def section_cadence(rep: Report, con, meta: dict, tz: ZoneInfo) -> None:
    rep.h("SAMPLING CADENCE (most recent rows of each time-indexed table)")
    rep("Top gaps between consecutive distinct timestamps; reveals per-minute vs event tables.")
    for t, m in meta.items():
        if not m["ts"]:
            continue
        col, unit = m["ts"]
        div = {"s": 1, "ms": 1e3, "us": 1e6, "ns": 1e9}[unit]
        vals = [r[0] for r in con.execute(
            f"SELECT DISTINCT {q(col)} FROM {q(t)} WHERE {q(col)} IS NOT NULL "
            f"ORDER BY 1 DESC LIMIT {CADENCE_ROWS}")]
        if len(vals) < 3:
            continue
        vals.reverse()
        deltas = [round((b - a) / div) for a, b in zip(vals, vals[1:])]
        top = Counter(deltas).most_common(5)
        med = statistics.median(deltas)
        rep(f"  {t:<44} n={len(vals):>7,}  median Δ={med:>8.0f}s  top Δ(s): "
            + ", ".join(f"{d}×{k:,}" for d, k in top))


def section_hr(rep: Report, con, meta: dict, tz: ZoneInfo, days: int) -> None:
    rep.h("HEART-RATE COLUMNS: value sanity and daily coverage (non-wear)")
    rep(f"'valid' = {HR_VALID[0]}..{HR_VALID[1]} bpm. Coverage = distinct local minutes per day with a "
        f"valid HR (1440 = full day).")
    for t, m in meta.items():
        if not m["ts"]:
            continue
        col, unit = m["ts"]
        div = {"s": 1, "ms": 1e3, "us": 1e6, "ns": 1e9}[unit]
        for c in m["cols"]:
            name = c["name"]
            if not HR_NAME.search(name) or m["stats"][name]["max"] is None:
                continue
            rows = con.execute(f"SELECT {q(col)}, {q(name)} FROM {q(t)} WHERE {q(col)} IS NOT NULL")
            total = 0
            valid: list[int] = []
            sentinel = Counter()
            per_day: dict[dt.date, set[int]] = {}
            for ts, hr in rows:
                total += 1
                if hr is not None and HR_VALID[0] <= hr <= HR_VALID[1]:
                    valid.append(hr)
                    local = dt.datetime.fromtimestamp(ts / div, tz=dt.timezone.utc).astimezone(tz)
                    per_day.setdefault(local.date(), set()).add(local.hour * 60 + local.minute)
                else:
                    sentinel[hr] += 1
            rep("")
            rep(f"  {t}.{name}: {total:,} rows, {len(valid):,} valid ({100 * len(valid) / max(total, 1):.1f}%)")
            rep("    invalid/sentinel values (top): " +
                ", ".join(f"{v!r}×{k:,}" for v, k in sentinel.most_common(6)))
            if len(valid) >= 20:
                qs = statistics.quantiles(valid, n=100)
                rep(f"    valid HR: min {min(valid)}, p1 {qs[0]:.0f}, p5 {qs[4]:.0f}, median "
                    f"{statistics.median(valid):.0f}, p95 {qs[94]:.0f}, p99 {qs[98]:.0f}, max {max(valid)}")
            if per_day:
                cov = sorted((d, len(s)) for d, s in per_day.items())
                mins = [k for _, k in cov]
                if statistics.median(mins) < 60:
                    # Daily-summary / event table: minutes-per-day would be meaningless.
                    rep(f"    ~{statistics.median(mins):.0f} valid values/day over {len(cov)} days "
                        f"(summary/event table; coverage not computed)")
                    continue
                rep(f"    days with any valid HR: {len(cov)}  ({cov[0][0]} -> {cov[-1][0]}); "
                    f"minutes/day min {min(mins)}, median {statistics.median(mins):.0f}, max {max(mins)}")
                rep(f"    last {days} days: " + "  ".join(f"{d.isoformat()[5:]}:{k}" for d, k in cov[-days:]))


def section_sleep(rep: Report, con, meta: dict, tz: ZoneInfo, n: int) -> None:
    rep.h("SLEEP-RELATED TABLES: most recent rows in local time")
    for t, m in meta.items():
        if not SLEEP_NAME.search(t):
            continue
        order = q(m["ts"][0]) if m["ts"] else "rowid"
        rep(f"-- {t} (latest {n}, ascending)")
        rows = list(con.execute(f"SELECT * FROM {q(t)} ORDER BY {order} DESC LIMIT {n}"))[::-1]
        for r in rows:
            rep("  " + render_row(r, m, tz))


def section_hrv(rep: Report, con, meta: dict, counts: dict[str, int]) -> None:
    rep.h("RR-INTERVAL / HRV DETECTION")
    rep("Heuristic: table or column names matching HRV/RMSSD/SDNN/RR/RRI/IBI/BEAT/INTERVAL, plus any")
    rep("BLOB columns (a device may store raw beat-to-beat data as an opaque payload).")
    hits = []
    for t, n in sorted(counts.items()):
        table_hit = bool(HRV_NAME.search(t))
        for c in columns(con, t):
            if table_hit or HRV_NAME.search(c["name"]):
                nn = con.execute(f"SELECT COUNT({q(c['name'])}) FROM {q(t)}").fetchone()[0]
                nz = con.execute(f"SELECT COUNT(*) FROM {q(t)} WHERE {q(c['name'])} IS NOT NULL "
                                 f"AND {q(c['name'])} != 0").fetchone()[0]
                hits.append((t, c["name"], n, nn, nz))
    if hits:
        rep(f"  {'table':<44}{'column':<30}{'rows':>10}{'non-null':>10}{'non-zero':>10}")
        for t, c, n, nn, nz in hits:
            rep(f"  {t:<44}{c:<30}{n:>10,}{nn:>10,}{nz:>10,}")
    else:
        rep("  no table/column names matched.")

    blobs = [(t, c, meta[t]["stats"][c]["blob_len"]) for t in meta for c in meta[t]["stats"]
             if meta[t]["stats"][c]["blob_len"] is not None]
    rep("")
    rep("  BLOB columns with data: " + (", ".join(f"{t}.{c} (avg {b:.0f} B)" for t, c, b in blobs) or "none"))

    populated = [h for h in hits if h[4] > 0]
    rep("")
    if populated:
        rep("  VERDICT: candidate RR/HRV data present in: " +
            ", ".join(sorted({f'{t}.{c}' for t, c, *_ in populated})))
        rep("  -> confirm against samples.txt (units: ms intervals vs. device HRV score) before use.")
    else:
        rep("  VERDICT: NO populated RR-interval or HRV column found by name.")
        rep("  -> unless a BLOB above turns out to carry beat-to-beat data, HRV (RMSSD) is not")
        rep("     computable from this export; recovery must use resting/min HR + sleep instead.")


# ---------------------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------------------
def render_value(name: str, v, meta_t: dict, tz: ZoneInfo) -> str:
    if isinstance(v, bytes):
        return f"<blob {len(v)}B {v[:48].hex()}{'…' if len(v) > 48 else ''}>"
    if isinstance(v, (int, float)) and TS_NAME.search(name):
        unit = ts_unit(v)
        if unit:
            return f"{v} [{fmt_dt(v, unit, tz)}]"
    return repr(v)


def render_row(r: sqlite3.Row, meta_t: dict, tz: ZoneInfo, redact_on: bool = True) -> str:
    return redact(", ".join(f"{k}={render_value(k, r[k], meta_t, tz)}" for k in r.keys()), redact_on)


def write_samples(con, meta: dict, tz: ZoneInfo, n: int, redact_on: bool) -> str:
    out = []
    for t, m in meta.items():
        order = q(m["ts"][0]) if m["ts"] else "rowid"
        first = list(con.execute(f"SELECT * FROM {q(t)} ORDER BY {order} ASC LIMIT {n}"))
        last = list(con.execute(f"SELECT * FROM {q(t)} ORDER BY {order} DESC LIMIT {n}"))[::-1]
        out.append(f"### {t}")
        out.append(f"-- first {n} by {order}")
        for r in first:
            d = dict(r)
            if redact_on and t == "USER" and "NAME" in d:
                out.append("  <USER row redacted: see report DEVICES/USER section>")
                continue
            out.append("  " + render_row(r, m, tz, redact_on))
        out.append(f"-- last {n} by {order}")
        for r in last:
            if redact_on and t == "USER":
                continue
            out.append("  " + render_row(r, m, tz, redact_on))
        out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", type=Path, help="Gadgetbridge export (SQLite file named 'Gadgetbridge')")
    ap.add_argument("--tz", default=None, help="IANA timezone for local dates (default: system local)")
    ap.add_argument("--out", type=Path, default=Path("inspect_out"), help="output directory")
    ap.add_argument("--samples", type=int, default=5, help="rows per table (first N and last N)")
    ap.add_argument("--days", type=int, default=21, help="recent days to list in HR coverage")
    ap.add_argument("--no-redact", action="store_true", help="do not mask MACs / user name")
    args = ap.parse_args(argv)

    if not args.db.is_file():
        ap.error(f"not a file: {args.db}")
    with open(args.db, "rb") as fh:
        if fh.read(16) != b"SQLite format 3\x00":
            ap.error(f"not an SQLite database: {args.db}")
    if args.tz:
        tz = ZoneInfo(args.tz)
    else:
        local = dt.datetime.now().astimezone().tzinfo
        key = getattr(local, "key", None)
        if not key:
            ap.error("could not determine an IANA system timezone; pass --tz, e.g. --tz Europe/Rome")
        tz = ZoneInfo(key)
    redact_on = not args.no_redact

    con = connect_ro(args.db)
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {q(t)}").fetchone()[0] for t in tables(con)}

    rep = Report()
    rep(f"Gadgetbridge inspection — generated {dt.datetime.now(tz).isoformat(timespec='seconds')}")
    section_overview(rep, con, args.db, tz, counts)
    section_devices(rep, con, counts, redact_on)
    meta = section_tables(rep, con, tz, counts)
    section_cadence(rep, con, meta, tz)
    section_hr(rep, con, meta, tz, args.days)
    section_sleep(rep, con, meta, tz, 12)
    section_hrv(rep, con, meta, counts)

    args.out.mkdir(parents=True, exist_ok=True)
    report = redact(rep.text(), redact_on)
    (args.out / "report.txt").write_text(report, encoding="utf-8")
    (args.out / "schema.sql").write_text(dump_schema(con), encoding="utf-8")
    (args.out / "samples.txt").write_text(write_samples(con, meta, tz, args.samples, redact_on),
                                          encoding="utf-8")
    sys.stdout.write(report)
    print(f"\nWrote {args.out}/report.txt, schema.sql, samples.txt", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
