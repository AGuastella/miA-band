"""Mi Fitness export adapter (docs/SPEC.md §9.2).

Reads the unzipped export folder:
  *_MiFitness_hlth_center_fitness_data.csv   Uid,Sid,Key,Time,Value(JSON),UpdateTime
  *_MiFitness_hlth_center_sport_record.csv   Uid,Sid,Key,Time,Category,Value(JSON),UpdateTime

Key -> canonical mapping:
  heart_rate                         -> hr_samples (background)
  single_heart_rate                  -> hr_samples (spot)
  watch_night_sleep, sleep           -> sleep_sessions + sleep_segments
  watch_daytime_sleep                -> sleep_sessions (is_nap=1) + segments
  resting_heart_rate                 -> device_daily (metric 'rhr'), reference only
  sport_record rows                  -> workouts (HR-zone seconds for Edwards TRIMP)
Everything else (steps, calories, stress, PAI, vitality, ...) is ignored for now.

Data-quality rules applied here, each reported in batch.notes:
  * rows before 2015-01-01 or in the future are dropped (unset device clock writes 2000-12-31);
  * sleep stage codes map 0 asleep / 2 deep / 3 light / 4 REM / 5 awake; any other code aborts
    the import; the mapping is verified night by night against the device's own per-stage
    durations and the agreement rate is reported (deep/light below 95 % aborts);
  * `timezone` is in 15-minute units; devices that never change offset across EU summer-time
    switches are detected as DST-naive and corrected for offsets UTC+0/+1.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from ..canonical import TABLES, CanonicalBatch

SOURCE = "mifitness"
MIN_TS = int(dt.datetime(2015, 1, 1, tzinfo=dt.timezone.utc).timestamp())
STAGE_CODES = {0: "asleep", 2: "deep", 3: "light", 4: "rem", 5: "awake"}
STAGE_FIELD = {2: "sleep_deep_duration", 3: "sleep_light_duration", 4: "sleep_rem_duration",
               5: "sleep_awake_duration"}
SLEEP_KEYS = {"watch_night_sleep", "sleep", "watch_daytime_sleep"}
ZONE_FIELDS = ("hrm_warm_up_duration", "hrm_fat_burning_duration", "hrm_aerobic_duration",
               "hrm_anaerobic_duration", "hrm_extreme_duration")

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


class ImportError_(RuntimeError):
    """The export contradicts an assumption the metrics rely on; refuse rather than guess."""


def find_file(folder: Path, suffix: str) -> Path | None:
    hits = sorted(folder.rglob(f"*_MiFitness_{suffix}.csv"))
    return hits[-1] if hits else None


def _nz(v):
    """Device placeholders (0, missing) -> None."""
    return None if v in (None, 0, "") else v


# ---------------------------------------------------------------------------------------
def _sleep_record(key: str, sid: str, d: dict, stats: Counter, bad_codes: Counter,
                  verify: dict) -> tuple[dict | None, list[dict]]:
    items = d.get("items") or []
    segs = []
    for it in items:
        s, e, code = it.get("start_time"), it.get("end_time"), it.get("state")
        if not (s and e) or e <= s:
            stats["sleep items without valid times (skipped)"] += 1
            continue
        if code not in STAGE_CODES:
            bad_codes[code] += 1
            continue
        segs.append(dict(start_ts=int(s), end_ts=int(e), stage=STAGE_CODES[code], code=code))
    start, end = _nz(d.get("bedtime")), _nz(d.get("wake_up_time"))
    if not (start and end and end > start):
        if not segs:
            stats["sleep records with no times and no items (skipped)"] += 1
            return None, []
        start, end = min(s["start_ts"] for s in segs), max(s["end_ts"] for s in segs)
    start, end = int(start), int(end)
    if start < MIN_TS:
        stats["sleep records before 2015 (dropped)"] += 1
        return None, []

    # verify the code mapping against the device's per-stage totals
    if segs and not any(s["code"] == 0 for s in segs):
        mins = Counter()
        for s in segs:
            mins[s["code"]] += (s["end_ts"] - s["start_ts"]) / 60
        for code, fld in STAGE_FIELD.items():
            dev = d.get(fld)
            if dev is None or (code in (4, 5) and not dev):
                continue
            verify[code][0] += 1
            verify[code][1] += abs(mins.get(code, 0) - dev) <= 1.0

    tz = d.get("timezone")
    session = dict(
        source=SOURCE, device_id=sid, start_ts=start, end_ts=end,
        is_nap=1 if key == "watch_daytime_sleep" else None,
        in_bed_start_ts=_nz(d.get("bed_timestamp")), in_bed_end_ts=_nz(d.get("out_bed_timestamp")),
        tz_offset_min=int(tz) * 15 if tz is not None else None,
        dev_duration_min=_nz(d.get("duration")), dev_deep_min=d.get("sleep_deep_duration"),
        dev_light_min=d.get("sleep_light_duration"), dev_rem_min=d.get("sleep_rem_duration"),
        dev_awake_min=d.get("sleep_awake_duration"), dev_min_hr=_nz(d.get("min_hr")),
        dev_avg_hr=_nz(d.get("avg_hr")))
    for s in segs:
        s.update(source=SOURCE, device_id=sid, session_start_ts=start)
        del s["code"]
    return session, segs


def read_fitness_data(path: Path, now_ts: int, notes: list[str]) -> dict[str, pd.DataFrame]:
    hr_ts, hr_bpm, hr_dev, hr_ctx = [], [], [], []
    sessions, segments, daily = [], [], []
    stats, bad_codes = Counter(), Counter()
    verify = defaultdict(lambda: [0, 0])
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rd = csv.reader(fh)
        header = next(rd)
        ix = {name: i for i, name in enumerate(header)}
        try:
            i_sid, i_key, i_time, i_val = ix["Sid"], ix["Key"], ix["Time"], ix["Value"]
        except KeyError as e:
            raise ImportError_(f"{path.name}: unexpected header {header}") from e
        for row in rd:
            key = row[i_key]
            if key in ("heart_rate", "single_heart_rate"):
                d = json.loads(row[i_val])
                ts, bpm = d.get("time"), d.get("bpm")
                if ts is None or bpm is None:
                    stats[f"{key} rows without time/bpm"] += 1
                    continue
                if not MIN_TS <= ts <= now_ts:
                    stats["HR rows with an impossible date (dropped)"] += 1
                    continue
                hr_ts.append(ts); hr_bpm.append(bpm); hr_dev.append(row[i_sid])
                hr_ctx.append("background" if key == "heart_rate" else "spot")
            elif key in SLEEP_KEYS:
                session, segs = _sleep_record(key, row[i_sid], json.loads(row[i_val]), stats, bad_codes, verify)
                if session:
                    sessions.append(session)
                    segments.extend(segs)
            elif key == "resting_heart_rate":
                d = json.loads(row[i_val])
                if d.get("date_time") and d.get("bpm"):
                    # date_time is 00:00 UTC of the local calendar date it describes
                    day = dt.datetime.fromtimestamp(d["date_time"], dt.timezone.utc).date().isoformat()
                    daily.append(dict(source=SOURCE, device_id=row[i_sid], date=day, metric="rhr",
                                      value=float(d["bpm"])))

    if bad_codes:
        raise ImportError_(f"unknown sleep stage codes {dict(bad_codes)}: refusing to guess a mapping")
    for code, (n, ok) in sorted(verify.items()):
        rate = ok / n if n else float("nan")
        notes.append(f"stage code {code} ({STAGE_CODES[code]}): items match device total on {ok}/{n} nights ({rate:.1%})")
        if code in (2, 3) and n >= 20 and rate < 0.95:
            raise ImportError_(f"stage code {code} disagrees with the device totals on {1 - rate:.0%} of nights")
        if code in (4, 5) and n >= 20 and rate < 0.80:
            notes.append(f"WARNING: inferred mapping for code {code} looks wrong; check before trusting stages")
    for k, v in sorted(stats.items()):
        notes.append(f"{k}: {v:,}")

    hr = pd.DataFrame({"source": SOURCE, "device_id": hr_dev, "ts": hr_ts, "bpm": hr_bpm, "context": hr_ctx})
    hr = hr.drop_duplicates(["device_id", "ts"], keep="last")
    sess = pd.DataFrame(sessions, columns=list(TABLES["sleep_sessions"]["columns"]))
    # the same sleep may be stored twice under one start (e.g. night + daytime key): keep the longest
    sess = (sess.assign(_len=sess["end_ts"] - sess["start_ts"]).sort_values("_len")
            .drop_duplicates(["device_id", "start_ts"], keep="last").drop(columns="_len"))
    seg = pd.DataFrame(segments, columns=list(TABLES["sleep_segments"]["columns"]))
    seg = seg.merge(sess[["device_id", "start_ts", "end_ts"]].rename(
        columns={"start_ts": "session_start_ts", "end_ts": "_send"}), on=["device_id", "session_start_ts"])
    seg = seg.drop(columns="_send").drop_duplicates(["device_id", "start_ts", "session_start_ts"])
    dd = pd.DataFrame(daily, columns=list(TABLES["device_daily"]["columns"]))
    dd = dd.drop_duplicates(["device_id", "date", "metric"], keep="last")
    return {"hr_samples": hr, "sleep_sessions": sess, "sleep_segments": seg, "device_daily": dd}


def read_sport_record(path: Path, now_ts: int, notes: list[str]) -> pd.DataFrame:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            d = json.loads(r["Value"])
            start = d.get("start_time") or d.get("time")
            end = d.get("end_time") or (start + d["duration"] if start and d.get("duration") else None)
            if not (start and end and end > start and MIN_TS <= start <= now_ts):
                continue
            tz = d.get("timezone")
            zones = [d.get(f) for f in ZONE_FIELDS]
            rows.append(dict(
                source=SOURCE, device_id=r["Sid"], start_ts=int(start), end_ts=int(end), sport=r["Key"],
                tz_offset_min=int(tz) * 15 if tz is not None else None,
                avg_hr=_nz(d.get("avg_hrm")), max_hr=_nz(d.get("max_hrm")), min_hr=_nz(d.get("min_hrm")),
                **{f"zone{i + 1}_s": (float(z) if z is not None else None) for i, z in enumerate(zones)},
                dev_train_load=d.get("train_load")))
    df = pd.DataFrame(rows, columns=list(TABLES["workouts"]["columns"]))
    df = df.drop_duplicates(["device_id", "start_ts"], keep="last")
    notes.append(f"workouts: {len(df):,} ({df['zone1_s'].notna().sum():,} with HR-zone durations)")
    return df


# ---------------------------------------------------------------------------------------
def _eu_switches(year: int) -> tuple[int, int]:
    """UTC instants of the EU summer-time start/end (last Sunday Mar/Oct, 01:00 UTC)."""
    out = []
    for month in (3, 10):
        last = dt.date(year, month, 31)
        sunday = last - dt.timedelta(days=(last.weekday() + 1) % 7)
        out.append(int(dt.datetime.combine(sunday, dt.time(1), dt.timezone.utc).timestamp()))
    return out[0], out[1]


def in_eu_summer_time(ts: np.ndarray) -> np.ndarray:
    ts = np.asarray(ts, dtype="int64")
    years = pd.to_datetime(ts, unit="s").year.to_numpy()
    out = np.zeros(ts.shape, dtype=bool)
    for y in np.unique(years):
        a, b = _eu_switches(int(y))
        m = years == y
        out[m] = (ts[m] >= a) & (ts[m] < b)
    return out


def detect_dst_naive(records: pd.DataFrame, window_days: int = 10, min_switches: int = 3) -> dict[str, tuple[bool, int, int]]:
    """Per device: does the recorded offset ever change across EU DST switches?

    For each switch with records on both sides (±window_days), compare the median offset
    before and after. A device that changed at < 20 % of >= min_switches evaluated switches
    is DST-naive (stores the standard offset all year). Returns {device: (naive, changed, n)}.
    """
    out = {}
    r = records.dropna(subset=["tz_offset_min"])
    for dev, g in r.groupby("device_id"):
        ts, off = g["start_ts"].to_numpy(dtype="int64"), g["tz_offset_min"].to_numpy()
        changed = n = 0
        years = range(pd.Timestamp(ts.min(), unit="s").year, pd.Timestamp(ts.max(), unit="s").year + 1)
        for y in years:
            for sw in _eu_switches(y):
                before = off[(ts >= sw - window_days * 86400) & (ts < sw)]
                after = off[(ts > sw) & (ts <= sw + window_days * 86400)]
                if len(before) >= 3 and len(after) >= 3:
                    n += 1
                    changed += np.median(before) != np.median(after)
        out[dev] = (bool(n >= min_switches and changed / n < 0.2), int(changed), int(n))
    return out


def fix_dst_naive(tables: dict[str, pd.DataFrame], notes: list[str]) -> None:
    both = pd.concat([tables["sleep_sessions"][["device_id", "start_ts", "tz_offset_min"]],
                      tables["workouts"][["device_id", "start_ts", "tz_offset_min"]]])
    verdict = detect_dst_naive(both)
    for dev, (naive, changed, n) in verdict.items():
        tag = dev[-6:]
        notes.append(f"device …{tag}: offset changed at {changed}/{n} EU DST switches -> "
                     + ("DST-naive, corrected" if naive else "DST-aware"))
        if not naive:
            continue
        for name in ("sleep_sessions", "workouts"):
            df = tables[name]
            m = ((df["device_id"] == dev) & df["tz_offset_min"].isin([0, 60])
                 & in_eu_summer_time(df["start_ts"].to_numpy()))
            df.loc[m, "tz_offset_min"] = df.loc[m, "tz_offset_min"] + 60


def read_export(folder: Path | str, now: dt.datetime | None = None) -> CanonicalBatch:
    folder = Path(folder)
    now_ts = int((now or dt.datetime.now(dt.timezone.utc)).timestamp()) + 86400
    notes: list[str] = []
    main = find_file(folder, "hlth_center_fitness_data")
    if main is None:
        raise ImportError_(f"no *_MiFitness_hlth_center_fitness_data.csv under {folder}")
    tables = read_fitness_data(main, now_ts, notes)
    sport = find_file(folder, "hlth_center_sport_record")
    tables["workouts"] = (read_sport_record(sport, now_ts, notes) if sport
                          else pd.DataFrame(columns=list(TABLES["workouts"]["columns"])))
    fix_dst_naive(tables, notes)
    for name, df in tables.items():
        notes.append(f"{name}: {len(df):,} rows")
    batch = CanonicalBatch(source=SOURCE, tables=tables, notes=notes)
    batch.validate()
    return batch


# ---------------------------------------------------------------------------------------
def write_export(batch: CanonicalBatch, folder: Path | str, prefix: str = "20261001_1_MiFitness_") -> None:
    """Inverse of read_export for canonical data (used by tests and the synthetic generator):
    writes the two CSVs in Mi Fitness layout with 'sleep' (new-format) records."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    code = {v: k for k, v in STAGE_CODES.items()}
    with open(folder / f"{prefix}hlth_center_fitness_data.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Uid", "Sid", "Key", "Time", "Value", "UpdateTime"])
        for r in batch.get("hr_samples").itertuples(index=False):
            key = "heart_rate" if r.context == "background" else "single_heart_rate"
            w.writerow(["1", r.device_id, key, int(r.ts), json.dumps({"time": int(r.ts), "bpm": int(r.bpm), "type": 0}), int(r.ts)])
        seg = batch.get("sleep_segments")
        for s in batch.get("sleep_sessions").itertuples(index=False):
            items = seg[(seg["device_id"] == s.device_id) & (seg["session_start_ts"] == s.start_ts)]
            mins = Counter()
            for it in items.itertuples(index=False):
                mins[it.stage] += (it.end_ts - it.start_ts) / 60
            rec = {"bedtime": int(s.start_ts), "wake_up_time": int(s.end_ts),
                   "duration": int((s.end_ts - s.start_ts) / 60),
                   "timezone": int(s.tz_offset_min // 15) if not pd.isna(s.tz_offset_min) else None,
                   "sleep_deep_duration": int(mins["deep"]), "sleep_light_duration": int(mins["light"]),
                   "sleep_rem_duration": int(mins["rem"]), "sleep_awake_duration": int(mins["awake"]),
                   "items": [{"start_time": int(i.start_ts), "end_time": int(i.end_ts), "state": code[i.stage]}
                             for i in items.itertuples(index=False)]}
            key = "watch_daytime_sleep" if s.is_nap == 1 else "sleep"
            w.writerow(["1", s.device_id, key, int(s.end_ts), json.dumps(rec), int(s.end_ts)])
    with open(folder / f"{prefix}hlth_center_sport_record.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Uid", "Sid", "Key", "Time", "Category", "Value", "UpdateTime"])
        for r in batch.get("workouts").itertuples(index=False):
            rec = {"start_time": int(r.start_ts), "end_time": int(r.end_ts),
                   "timezone": int(r.tz_offset_min // 15), "avg_hrm": r.avg_hr, "max_hrm": r.max_hr,
                   "min_hrm": r.min_hr, **{f: getattr(r, f"zone{i + 1}_s") for i, f in enumerate(ZONE_FIELDS)}}
            w.writerow(["1", r.device_id, r.sport, int(r.start_ts), r.sport, json.dumps(rec), int(r.end_ts)])
