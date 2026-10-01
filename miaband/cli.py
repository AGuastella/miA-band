"""Command line: import -> compute -> show / sanity.

  python -m miaband import-mifitness data/mifitness      # idempotent; re-run on every new export
  python -m miaband compute                              # full recompute of daily metrics
  python -m miaband show [--days 7]                      # today + the last N days
  python -m miaband sanity [--days 60]                   # our numbers vs the band's own
  python -m miaband demo                                 # synthetic data into data/demo.sqlite
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from . import pipeline, store
from .config import Config, load_config

HRV_NOTICE = ("HRV: not available from this source (no beat-to-beat data). "
              "Recovery will use resting HR + sleep only.")


def _hhmm(m) -> str:
    if m is None or pd.isna(m):
        return "  -  "
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _hm(m) -> str:
    if m is None or pd.isna(m):
        return "   -  "
    m = int(round(m))
    return f"{m // 60}h{m % 60:02d}"


def _num(v, fmt="{:.0f}") -> str:
    return "-" if v is None or pd.isna(v) else fmt.format(v)


def cmd_import_mifitness(cfg: Config, args) -> int:
    from .sources import mifitness
    folder = Path(args.folder)
    main = mifitness.find_file(folder, "hlth_center_fitness_data")
    con = store.connect(cfg.store)
    digest = store.file_sha256(main) if main else None
    if digest and store.already_ingested(con, digest):
        print("note: this exact export was imported before; re-importing (idempotent).")
    print(f"reading {folder} ...", file=sys.stderr)
    batch = mifitness.read_export(folder)
    print("\n".join("  " + n for n in batch.notes))
    written = store.write_batch(con, batch, digest)
    print("written: " + ", ".join(f"{k}={v:,}" for k, v in written.items()))
    print("next: python -m miaband compute")
    return 0


def cmd_compute(cfg: Config, args) -> int:
    con = store.connect(cfg.store)
    wide = pipeline.run(con, cfg)
    if wide.empty or "episode" not in wide:
        print("no sleep data in the store yet")
        return 1
    nights = wide["episode"].notna().sum()
    print(f"computed {len(wide)} days ({nights} with a main sleep), "
          f"{wide['date'].min():%Y-%m-%d} -> {wide['date'].max():%Y-%m-%d}")
    return 0


def _daily_wide(con) -> pd.DataFrame:
    d = store.read_table(con, "daily_metrics")
    if d.empty:
        return d
    val = d.pivot(index="date", columns="metric", values="value")
    st = d.pivot(index="date", columns="metric", values="status").add_suffix(":status")
    why = d.pivot(index="date", columns="metric", values="reason").add_suffix(":reason")
    return pd.concat([val, st, why], axis=1).sort_index()


def cmd_show(cfg: Config, args) -> int:
    con = store.connect(cfg.store)
    w = _daily_wide(con)
    if w.empty:
        print("no daily metrics: run `python -m miaband compute` first")
        return 1
    w = w.tail(args.days + 1)
    print(HRV_NOTICE)
    print()
    hdr = (f"{'night of':<11} {'sleep':>6} {'bed':>5}-{'wake':<5} {'SME':>4} {'deep/light/REM':>15} "
           f"{'nap':>4} {'SRI7':>5} {'RHR':>4} {'RHR z':>6}  notes")
    print(hdr)
    print("-" * len(hdr))
    for date, r in w.iterrows():
        notes = []
        if r.get("sleep.tst_min:status") != "ok":
            print(f"{date:<11} {'—':>6}  insufficient data: {r.get('sleep.tst_min:reason')}")
            continue
        stages = (f"{_num(r['sleep.deep_min'])}/{_num(r['sleep.light_min'])}/{_num(r['sleep.rem_min'])}"
                  if r.get("sleep.rem_min:status") == "ok" else "n/a")
        rhr = _num(r.get("phys.rhr"))
        z = r.get("rhr.z")
        if r.get("rhr.z:status") == "ok":
            zs = f"{z:+.1f}"
            if r.get("rhr.flag") == 1:
                notes.append("RHR above baseline")
        else:
            zs = {"warming_up": "warm", "insufficient_data": "n/a"}.get(r.get("rhr.z:status"), "n/a")
        if r.get("tst.flag") == 1 and r.get("tst.z:status") == "ok":
            notes.append("short vs baseline")
        if r.get("phys.rhr:status") != "ok":
            notes.append(f"RHR: {r.get('phys.rhr:reason')}")
        print(f"{date:<11} {_hm(r['sleep.tst_min']):>6} {_hhmm(r['sleep.onset_clock'])}-{_hhmm(r['sleep.wake_clock'])} "
              f"{_num(r['sleep.sme'] * 100 if not pd.isna(r['sleep.sme']) else np.nan):>3}% {stages:>15} "
              f"{_num(r['sleep.nap_min']):>4} {_num(r.get('sleep.sri_7d')):>5} {rhr:>4} {zs:>6}  {'; '.join(notes)}")
    last = w.iloc[-1]
    print()
    print("7-day means: sleep " + _hm(last.get("tst.mean7")) + ", RHR " + _num(last.get("rhr.mean7"), "{:.1f}")
          + " | 28-day baseline: RHR " + _num(last.get("rhr.base28"), "{:.1f}")
          + ", sleep " + _hm(last.get("tst.base28")))
    print("SME = sleep maintenance efficiency (TST / sleep period); the band has no real time-in-bed.")
    return 0


def cmd_sanity(cfg: Config, args) -> int:
    """Our nightly values next to the band's own, for the last N nights."""
    con = store.connect(cfg.store)
    tables = pipeline.load_tables(con)
    wide = pipeline.compute_nightly(tables, cfg)
    if "episode" not in wide:
        print("no nights to compare: import an export first")
        return 1
    nights = wide[wide["episode"].notna()].tail(args.days).copy()
    if nights.empty:
        print("no nights to compare")
        return 1
    ses = tables["sleep_sessions"]
    dev_daily = store.read_table(con, "device_daily", "WHERE metric = 'rhr'")
    dev_rhr = dev_daily.groupby("date")["value"].mean()
    rows = []
    for n in nights.itertuples(index=False):
        members = ses[(ses["start_ts"] >= n.onset_ts) & (ses["end_ts"] <= n.wake_ts) & (ses["source"] == n.source)]
        rows.append(dict(
            date=f"{n.date:%Y-%m-%d}", tst=n.tst_min, dev_tst=members["dev_duration_min"].sum(min_count=1),
            rhr=n.rhr, dev_sleep_min_hr=members["dev_min_hr"].min(), dev_rhr=dev_rhr.get(f"{n.date:%Y-%m-%d}"),
            onset=_hhmm(n.onset_clock), wake=_hhmm(n.wake_clock)))
    df = pd.DataFrame(rows)
    print(f"{'night':<11} {'TST ours':>9} {'band':>6} {'Δmin':>5}   {'bed-wake':<11} "
          f"{'RHR ours':>8} {'band sleep min':>14} {'band RHR':>8}")
    for r in df.itertuples(index=False):
        diff = r.tst - r.dev_tst if not pd.isna(r.dev_tst) else np.nan
        print(f"{r.date:<11} {_hm(r.tst):>9} {_hm(r.dev_tst):>6} {_num(diff, '{:+.0f}'):>5}   {r.onset}-{r.wake} "
              f"{_num(r.rhr, '{:.1f}'):>8} {_num(r.dev_sleep_min_hr):>14} {_num(r.dev_rhr):>8}")
    d = (df["tst"] - df["dev_tst"]).dropna()
    print()
    if len(d):
        print(f"TST vs band: mean Δ {d.mean():+.1f} min, |Δ| ≤ 15 min on {(d.abs() <= 15).mean():.0%} of {len(d)} nights")
    r = (df["rhr"] - df["dev_sleep_min_hr"]).dropna()
    if len(r):
        print(f"our RHR − band's single-sample sleep minimum: mean {r.mean():+.1f} bpm "
              "(expected ≥ 0: ours is a 10-min mean, theirs a single sample)")
    r = (df["rhr"] - df["dev_rhr"]).dropna()
    if len(r):
        print(f"our RHR − band's daily 'resting HR': mean {r.mean():+.1f} bpm "
              "(expected < 0: ours is the sleeping nadir)")
    print("Compare a few nights with the sleep screen in Mi Fitness, too.")
    return 0


def cmd_demo(cfg: Config, args) -> int:
    from .sources.synthetic import Scenario, generate
    today = dt.date.today()
    sc = Scenario(first_wake=today - dt.timedelta(days=41), nights=42, bedtime_jitter_min=25, noise_sd=1.5,
                  short_nights={today - dt.timedelta(days=3): 5.0},
                  rhr_delta={today - dt.timedelta(days=1): 7},
                  missing_nights={today - dt.timedelta(days=5)},
                  naps={today - dt.timedelta(days=2): (dt.time(15, 30), 35)})
    batch, _ = generate(sc)
    path = Path(args.db)
    con = store.connect(path)
    store.write_batch(con, batch)
    pipeline.run(con, cfg)
    print(f"demo data written to {path}; showing it:\n")
    return cmd_show(Config(**{**cfg.__dict__, "store": path}), args)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="miaband", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.toml", help="TOML config (default: config.toml if present)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import-mifitness"); p.add_argument("folder")
    sub.add_parser("compute")
    p = sub.add_parser("show"); p.add_argument("--days", type=int, default=7)
    p = sub.add_parser("sanity"); p.add_argument("--days", type=int, default=30)
    p = sub.add_parser("demo"); p.add_argument("--db", default="data/demo.sqlite"); p.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    return {"import-mifitness": cmd_import_mifitness, "compute": cmd_compute, "show": cmd_show,
            "sanity": cmd_sanity, "demo": cmd_demo}[args.cmd](cfg, args)
