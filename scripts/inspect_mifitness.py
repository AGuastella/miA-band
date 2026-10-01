#!/usr/bin/env python3
"""Inspect a Mi Fitness data export (folder of CSVs) before writing the adapter.

Stdlib only, streaming (no file is loaded whole), no network. Writes to --out:

  report.txt   per file: rows, columns, null rates, time range; for key/value files
               (one row per Key with a JSON Value) a per-Key breakdown: rows, time range,
               rows per year, device ids, sampling cadence, duplicate timestamps, and the
               full structure of the JSON payload (every path, types, ranges, distinct values).
               Ends with an RR/HRV verdict and a device-switch summary.
  samples.txt  a few raw rows per file and per Key (JSON truncated), timestamps also in local time.

Usage:
  python scripts/inspect_mifitness.py path/to/unzipped_export --out inspect_mifitness_out

Privacy (on by default, --no-redact to disable): user/account/device ids are replaced by a
short stable hash (so a device switch stays visible), GPS-like fields and the GPS track file's
values are never printed, and the account id embedded in file names is masked.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import statistics
import sys
from array import array
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

ID_COL = re.compile(r"(uid|user|account|did|mac|serial|(^|_)sn$|sid)", re.I)
GEO = re.compile(r"(lat|lon|lng|latitude|longitude|location|gps|coordinate|address|altitude)", re.I)
JSON_ID = re.compile(r"(^|\.)(uid|did|sid|mac|sn|serial|device_?id|user_?id)$", re.I)
TIME_NAME = re.compile(r"(time|date|start|end|bedtime|wake|(^|_)ts$|timestamp|day)", re.I)
HRV_NAME = re.compile(r"(hrv|rmssd|sdnn|(^|[^a-z0-9])rri?([^a-z0-9]|$)|(^|_)ibi(_|$)|rr_?interval|beat_?to_?beat|interval)", re.I)
# Legacy records use per-file storage paths as keys ("2017/04/29/<account>/xiaomisports_app_...").
FILE_REF_KEY = re.compile(r"^\d{4}/\d{2}/\d{2}/")
KEY_COL_NAMES = ("key", "type", "data_type", "category")
VALUE_COL_NAMES = ("value", "data", "content", "detail")

MAX_PATHS_PER_KEY = 80
MAX_DISTINCT = 30            # print value counts when a path/column has at most this many values
MAX_LIST_ELEMS = 2000        # elements of a JSON list walked per row
SAMPLE_ROWS = 3
SAMPLE_JSON_CHARS = 2500


# ---------------------------------------------------------------------------------------
def ts_unit(v) -> str | None:
    try:
        v = abs(float(v))
    except (TypeError, ValueError):
        return None
    if v > 1e17:
        return "ns"
    if v > 1e14:
        return "us"
    if v > 1e11:
        return "ms"
    if v > 3e8:          # > 1979 in seconds
        return "s"
    return None


DIV = {"s": 1, "ms": 1e3, "us": 1e6, "ns": 1e9}


def to_epoch_s(v) -> float | None:
    u = ts_unit(v)
    return float(v) / DIV[u] if u else None


def fmt_local(epoch_s: float, tz: ZoneInfo) -> str:
    return dt.datetime.fromtimestamp(epoch_s, tz=dt.timezone.utc).astimezone(tz).isoformat(timespec="seconds")


def short_hash(s: str) -> str:
    return "id:" + hashlib.sha256(s.encode()).hexdigest()[:8]


def as_number(s):
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return s
    try:
        return int(s)
    except (TypeError, ValueError):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None


class Report:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, s=""):
        self.lines.append(s)

    def h(self, title):
        self.lines += ["", "=" * 92, title, "=" * 92]

    def text(self):
        return "\n".join(self.lines) + "\n"


# ---------------------------------------------------------------------------------------
# Value statistics
# ---------------------------------------------------------------------------------------
class ValStats:
    """Streaming stats for one column or one JSON path."""
    __slots__ = ("n", "empty", "types", "nmin", "nmax", "distinct", "overflow", "example", "ts_unit")

    def __init__(self):
        self.n = 0
        self.empty = 0
        self.types = Counter()
        self.nmin = None
        self.nmax = None
        self.distinct: Counter = Counter()
        self.overflow = False
        self.example = None
        self.ts_unit = None

    def add(self, v):
        self.n += 1
        if v is None or v == "":
            self.empty += 1
            return
        num = as_number(v)
        if isinstance(v, str) and num is None:
            self.types["str"] += 1
        else:
            self.types[type(v).__name__ if not isinstance(v, str) else "numstr"] += 1
        if num is not None:
            self.nmin = num if self.nmin is None else min(self.nmin, num)
            self.nmax = num if self.nmax is None else max(self.nmax, num)
        if self.example is None:
            self.example = v
        if not self.overflow:
            key = v if not isinstance(v, (dict, list)) else json.dumps(v)[:60]
            self.distinct[key] += 1
            if len(self.distinct) > MAX_DISTINCT:
                self.overflow = True
                self.distinct = Counter()

    def describe(self, name: str, tz: ZoneInfo, redact_values: bool) -> str:
        types = ",".join(f"{t}" for t, _ in self.types.most_common())
        out = f"n={self.n:,}"
        if self.empty:
            out += f" empty={100 * self.empty / max(self.n, 1):.1f}%"
        out += f" types={types or '-'}"
        if redact_values:
            return out + " values=<redacted>"
        if self.nmin is not None:
            unit = ts_unit(self.nmax) if TIME_NAME.search(name) else None
            if unit:
                out += (f" range {fmt_local(float(self.nmin) / DIV[unit], tz)} .. "
                        f"{fmt_local(float(self.nmax) / DIV[unit], tz)} [{unit}]")
            else:
                out += f" range {self.nmin} .. {self.nmax}"
        if not self.overflow and self.distinct:
            vals = sorted(self.distinct.items(), key=lambda kv: (-kv[1], str(kv[0])))
            out += " values{" + ", ".join(f"{v!r}:{c:,}" for v, c in vals) + "}"
        elif self.example is not None and self.nmin is None:
            ex = str(self.example)
            out += f" e.g. {ex[:60]!r}"
        return out


def walk_json(obj, path: str, sink, depth=0):
    """Flatten JSON into (path, scalar) pairs; lists become `path[]`, and their lengths
    are reported as `path[].#len`."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            walk_json(v, f"{path}.{k}" if path else k, sink, depth + 1)
    elif isinstance(obj, list):
        sink(f"{path}[].#len", len(obj))
        for el in obj[:MAX_LIST_ELEMS]:
            walk_json(el, f"{path}[]", sink, depth + 1)
    else:
        sink(path or "<scalar>", obj)


def parse_jsonish(s: str):
    s = s.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------------------
# Per-key aggregation for key/value files
# ---------------------------------------------------------------------------------------
class KeyAgg:
    def __init__(self):
        self.n = 0
        self.bad_json = 0
        self.ts_by_dev: dict[str, array] = defaultdict(lambda: array("d"))
        self.per_year = Counter()
        self.paths: dict[str, ValStats] = {}
        self.paths_overflow = 0
        self.first: list[dict] = []
        self.last: list[dict] = []

    def add_path(self, path, v):
        st = self.paths.get(path)
        if st is None:
            if len(self.paths) >= MAX_PATHS_PER_KEY:
                self.paths_overflow += 1
                return
            st = self.paths[path] = ValStats()
        st.add(v)


def detect_roles(header: list[str], probe: list[dict]) -> dict:
    """Pick the time, key, value (JSON) and device columns from a sample of rows."""
    lower = {c.lower(): c for c in header}
    roles = {"time": None, "key": None, "value": None, "device": None}

    for name in KEY_COL_NAMES:
        if name in lower:
            roles["key"] = lower[name]
            break
    json_share = {c: sum(1 for r in probe if parse_jsonish(r.get(c) or "") is not None) / max(len(probe), 1)
                  for c in header}
    for name in VALUE_COL_NAMES:
        if name in lower and json_share[lower[name]] > 0.3:
            roles["value"] = lower[name]
            break
    if roles["value"] is None:
        best = max(header, key=lambda c: json_share[c], default=None)
        if best and json_share[best] > 0.5:
            roles["value"] = best

    # Time: prefer a column literally named time/timestamp, with epoch-like numbers.
    time_cands = []
    for c in header:
        vals = [r.get(c) for r in probe if r.get(c)]
        if vals and TIME_NAME.search(c) and sum(1 for v in vals if ts_unit(v)) / len(vals) > 0.9:
            rank = 0 if c.lower() in ("time", "timestamp") else 1 if "update" not in c.lower() else 2
            time_cands.append((rank, c))
    roles["time"] = sorted(time_cands)[0][1] if time_cands else None

    for c in header:
        if c.lower() in ("sid", "did", "device_id", "deviceid", "device"):
            roles["device"] = c
            break
    return roles


def profile_file(path: Path, tz: ZoneInfo, redact_on: bool, rep: Report, samples: list[str],
                 global_findings: dict) -> None:
    is_geo_file = bool(re.search(r"track|gps", path.name, re.I))
    display_name = mask_name(path.name) if redact_on else path.name

    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        probe = []
        for r in reader:
            probe.append(r)
            if len(probe) >= 5000:
                break
    roles = detect_roles(header, probe)

    col_stats = {c: ValStats() for c in header}
    keys: dict[str, KeyAgg] = defaultdict(KeyAgg)
    file_ts = array("d")
    n = 0
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        for row in csv.DictReader(fh):
            n += 1
            for c in header:
                v = row.get(c)
                if c == roles["value"] and v and len(v) > 200:
                    v = v[:200]          # column stats only need a prefix of big JSON
                col_stats[c].add(v)
            ts = to_epoch_s(row.get(roles["time"])) if roles["time"] else None
            if ts is not None:
                file_ts.append(ts)
            if not (roles["key"] and roles["value"]):
                continue
            k = row.get(roles["key"]) or "<empty>"
            if FILE_REF_KEY.match(k):
                k = "<file-ref>"     # thousands of one-row path keys: report them as one bucket
            agg = keys[k]
            agg.n += 1
            dev = row.get(roles["device"]) or "-" if roles["device"] else "-"
            if ts is not None:
                agg.ts_by_dev[dev].append(ts)
                agg.per_year[dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).astimezone(tz).year] += 1
            obj = parse_jsonish(row.get(roles["value"]) or "")
            if obj is None:
                agg.bad_json += 1
                agg.add_path("<raw>", row.get(roles["value"]))
            else:
                walk_json(obj, "", agg.add_path)
            if len(agg.first) < SAMPLE_ROWS:
                agg.first.append(row)
            else:
                agg.last.append(row)
                if len(agg.last) > SAMPLE_ROWS:
                    agg.last.pop(0)

    # ---- file-level section
    rep.h(f"FILE {display_name}")
    rep(f"rows: {n:,}   columns: {len(header)}   size: {path.stat().st_size / 1e6:.1f} MB")
    rep(f"detected roles: " + ", ".join(f"{k}={v}" for k, v in roles.items()))
    if file_ts:
        rep(f"time range ({roles['time']}): {fmt_local(min(file_ts), tz)} -> {fmt_local(max(file_ts), tz)}")
    rep("columns:")
    for c in header:
        red = (is_geo_file and not TIME_NAME.search(c)) or bool(GEO.search(c)) or \
            (redact_on and bool(ID_COL.search(c)))
        desc = col_stats[c].describe(c, tz, red)
        if redact_on and ID_COL.search(c) and not col_stats[c].overflow:
            desc += f"  distinct ids={len(col_stats[c].distinct)}"
        if c == roles["value"]:
            desc = re.sub(r" (values\{.*\}|e\.g\. .*)$", "", desc) + "  (JSON payload, see per-key detail)"
        rep(f"  {c:<28} {desc}")
        if HRV_NAME.search(c):
            global_findings["hrv_hits"].append(f"{display_name}: column {c}")

    # ---- per-key section
    if keys:
        rep("")
        rep(f"Keys ({len(keys)}): " + ", ".join(f"{k}:{a.n:,}" for k, a in
                                               sorted(keys.items(), key=lambda kv: -kv[1].n)))
        for k, agg in sorted(keys.items()):
            report_key(rep, display_name, k, agg, tz, redact_on, is_geo_file, global_findings)

    # ---- samples
    samples.append(f"### {display_name}")
    if is_geo_file:
        samples.append("  <values not printed: GPS track file>")
    elif keys:
        for k, agg in sorted(keys.items()):
            samples.append(f"-- key={k!r} (first {len(agg.first)}, last {len(agg.last)})")
            for i, row in enumerate(agg.first + agg.last):
                # One complete record for sleep keys: the stage list is what the adapter needs.
                full = i == 0 and re.search(r"sleep", k, re.I)
                samples.append("  " + render_row(row, roles, tz, redact_on,
                                                 SAMPLE_JSON_CHARS * 8 if full else SAMPLE_JSON_CHARS))
    else:
        for row in probe[:SAMPLE_ROWS] + probe[-SAMPLE_ROWS:]:
            samples.append("  " + render_row(row, roles, tz, redact_on))
    samples.append("")


def report_key(rep, fname, k, agg: KeyAgg, tz, redact_on, is_geo_file, gf):
    rep("")
    rep(f"--- key {k!r}: {agg.n:,} rows" + (f", {agg.bad_json:,} non-JSON values" if agg.bad_json else ""))
    if HRV_NAME.search(k):
        gf["hrv_hits"].append(f"{fname}: key {k!r} ({agg.n:,} rows)")
    all_ts = sorted(t for a in agg.ts_by_dev.values() for t in a)
    if all_ts:
        rep(f"    time range: {fmt_local(all_ts[0], tz)} -> {fmt_local(all_ts[-1], tz)}")
        rep("    rows/year: " + ", ".join(f"{y}:{c:,}" for y, c in sorted(agg.per_year.items())))
    for dev, arr in sorted(agg.ts_by_dev.items(), key=lambda kv: min(kv[1]) if kv[1] else 0):
        if not arr:
            continue
        ts = sorted(arr)
        label = short_hash(dev) if (redact_on and dev != "-") else dev
        dup = sum(1 for a, b in zip(ts, ts[1:]) if a == b)
        deltas = [b - a for a, b in zip(ts, ts[1:]) if b > a]
        cad = ""
        if deltas:
            top = Counter(round(d) for d in deltas).most_common(4)
            recent = [b - a for a, b in zip(ts, ts[1:]) if b > a and b > ts[-1] - 90 * 86400]
            cad = (f"median Δ {statistics.median(deltas):.0f}s"
                   + (f" (last 90 d: {statistics.median(recent):.0f}s)" if recent else "")
                   + "; top Δ(s): " + ", ".join(f"{d}×{c:,}" for d, c in top))
        rep(f"    device {label}: {len(ts):,} rows {fmt_local(ts[0], tz)[:10]} -> {fmt_local(ts[-1], tz)[:10]}"
            f"; duplicate timestamps {dup:,}; {cad}")
        gf["devices"][label].append((k, ts[0], ts[-1], len(ts)))
    if agg.paths:
        rep("    JSON structure:")
        for p, st in agg.paths.items():
            red = is_geo_file or bool(GEO.search(p)) or (redact_on and bool(JSON_ID.search(p)))
            rep(f"      {p:<36} {st.describe(p, tz, red)}")
            if HRV_NAME.search(p) and st.n - st.empty > 0:
                gf["hrv_hits"].append(f"{fname}: key {k!r} path {p} ({st.n - st.empty:,} non-empty)")
        if agg.paths_overflow:
            rep(f"      ... {agg.paths_overflow:,} more path occurrences beyond {MAX_PATHS_PER_KEY} paths")


def render_row(row: dict, roles: dict, tz: ZoneInfo, redact_on: bool,
               max_chars: int = SAMPLE_JSON_CHARS) -> str:
    parts = []
    for c, v in row.items():
        if v is None:
            continue
        if redact_on and ID_COL.search(c) and v:
            v = short_hash(v)
        elif GEO.search(c):
            v = "<redacted>"
        elif c == roles["value"]:
            obj = parse_jsonish(v)
            if obj is not None:
                obj = scrub_geo(obj)
                v = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                v = add_local_times(v, tz)
            if len(v) > max_chars:
                v = v[:max_chars] + f"…<+{len(v) - max_chars} chars>"
        elif TIME_NAME.search(c) and ts_unit(v):
            v = f"{v} [{fmt_local(to_epoch_s(v), tz)}]"
        parts.append(f"{c}={v}")
    return ", ".join(parts)


def scrub_geo(obj):
    if isinstance(obj, dict):
        return {k: ("<redacted>" if GEO.search(k) else short_hash(str(v)) if JSON_ID.search(k)
                    else scrub_geo(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_geo(x) for x in obj]
    return obj


TIME_FIELD_RE = re.compile(r'"([A-Za-z_]*(?:time|start|end|bedtime|wake|ts|date)[A-Za-z_]*)":(\d{9,13})', re.I)


def add_local_times(s: str, tz: ZoneInfo) -> str:
    """Annotate epoch-like JSON time fields with local time, e.g. "bedtime":1727992800«2024-10-03T23:00»."""
    def sub(m):
        e = to_epoch_s(m.group(2))
        return m.group(0) + (f"«{fmt_local(e, tz)[:19]}»" if e else "")
    return TIME_FIELD_RE.sub(sub, s)


def mask_name(name: str) -> str:
    # <date>_<accountID>_MiFitness_<table>.csv -> <date>_<account>_MiFitness_<table>.csv
    return re.sub(r"^(\d+_)(\d+)(_)", r"\1<account>\3", name)


# ---------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export_dir", type=Path, help="folder containing the unzipped Mi Fitness CSVs")
    ap.add_argument("--tz", default="Europe/Madrid", help="IANA timezone for local rendering")
    ap.add_argument("--out", type=Path, default=Path("inspect_mifitness_out"))
    ap.add_argument("--no-redact", action="store_true")
    args = ap.parse_args(argv)

    if not args.export_dir.is_dir():
        ap.error(f"not a directory: {args.export_dir} (unzip the export first)")
    files = sorted(p for p in args.export_dir.rglob("*") if p.suffix.lower() == ".csv")
    if not files:
        ap.error(f"no .csv files under {args.export_dir}")
    tz = ZoneInfo(args.tz)
    redact_on = not args.no_redact

    rep = Report()
    rep(f"Mi Fitness export inspection — generated {dt.datetime.now(tz).isoformat(timespec='seconds')}"
        f"  (tz {tz.key})")
    rep(f"files ({len(files)}):")
    for p in files:
        rep(f"  {(mask_name(p.name) if redact_on else p.name):<70} {p.stat().st_size / 1e6:>9.1f} MB")

    samples: list[str] = []
    gf = {"hrv_hits": [], "devices": defaultdict(list)}
    for p in files:
        print(f"profiling {p.name} ...", file=sys.stderr)
        try:
            profile_file(p, tz, redact_on, rep, samples, gf)
        except Exception as e:  # keep going: one odd file shouldn't hide the others
            rep.h(f"FILE {mask_name(p.name)}")
            rep(f"  ERROR while profiling: {type(e).__name__}: {e}")

    rep.h("DEVICE IDS OVER TIME (from per-key device column)")
    if gf["devices"]:
        for dev, rows in sorted(gf["devices"].items(), key=lambda kv: min(r[1] for r in kv[1])):
            lo = min(r[1] for r in rows)
            hi = max(r[2] for r in rows)
            ks = ", ".join(sorted({r[0] for r in rows}))
            rep(f"  {dev}: {fmt_local(lo, tz)[:10]} -> {fmt_local(hi, tz)[:10]}  keys: {ks}")
    else:
        rep("  no device column detected")

    rep.h("RR-INTERVAL / HRV DETECTION")
    if gf["hrv_hits"]:
        rep("Candidates (name-based; confirm in samples.txt before trusting):")
        for h in gf["hrv_hits"]:
            rep(f"  {h}")
    else:
        rep("VERDICT: no key, column or JSON path name suggests RR intervals or HRV.")
        rep("-> HRV (RMSSD) is not computable from this export; recovery uses resting HR + sleep.")

    report, sample_text = rep.text(), "\n".join(samples) + "\n"
    if redact_on:
        # The account id also appears inside values (legacy storage paths), not just file names.
        accounts = {m.group(1) for p in files if (m := re.match(r"^\d+_(\d+)_", p.name))}
        for acc in accounts:
            report = report.replace(acc, "<account>")
            sample_text = sample_text.replace(acc, "<account>")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.txt").write_text(report, encoding="utf-8")
    (args.out / "samples.txt").write_text(sample_text, encoding="utf-8")
    sys.stdout.write(report)
    print(f"\nWrote {args.out}/report.txt and samples.txt", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
