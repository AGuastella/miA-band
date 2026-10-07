"""Command line: import -> compute -> show / sanity.

  python -m miaband import-mifitness data/mifitness      # idempotent; re-run on every new export
  python -m miaband compute                              # full recompute of daily metrics
  python -m miaband show [--days 7]                      # today + the last N days
  python -m miaband sanity [--days 60]                   # our numbers vs the band's own
  python -m miaband demo                                 # synthetic data into data/demo.sqlite
  python -m miaband serve                                # dashboard on http://127.0.0.1:8765
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
    try:
        batch = mifitness.read_export(folder)
    except mifitness.ImportError_ as e:
        print(f"import failed: {e}", file=sys.stderr)
        return 2
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


def _summary(date, r) -> None:
    print(f"=== {date} ===")
    st = r.get("recovery.score:status")
    if st == "ok":
        score = r["recovery.score"]
        band = "low" if score <= 33 else "high" if score >= 67 else "moderate"
        print(f"Recovery (resting HR + sleep, no HRV): {score:.0f}/100 ({band})")
        print(f"  resting HR {_num(r.get('phys.rhr'), '{:.1f}')} bpm vs baseline {_num(r.get('rhr.base28'), '{:.1f}')}"
              f" ±{_num(r.get('rhr.spread28'), '{:.1f}')} -> z {r.get('recovery.z_rhr'):+.2f} (higher RHR = negative)"
              f" -> {r.get('recovery.pts_rhr'):+.0f} pts")
        print(f"  sleep {_hm(r.get('sleep.tst_min'))} vs baseline {_hm(r.get('tst.base28'))}"
              f" ±{_num(r.get('tst.spread28'))} min -> z {r.get('recovery.z_sleep'):+.2f}"
              f" -> {r.get('recovery.pts_sleep'):+.0f} pts")
        print("  (score = 50 + the points above; weights in config [recovery])")
    else:
        print(f"Recovery: {st or 'n/a'} — {r.get('recovery.score:reason') or 'no data'}")
    rule = r.get("readiness.rule")
    if rule is not None and not pd.isna(rule):
        print(f"Readiness: {r.get('readiness.rule:reason')}   [rule {int(rule)}]")
    acwr = (f"{r['acwr.ewma']:.2f}" if r.get("acwr.ewma:status") == "ok"
            else f"n/a ({r.get('acwr.ewma:reason')})")
    print(f"Load today: {_num(r.get('strain.load'))} (strain {_num(r.get('strain.strain'), '{:.1f}')}), ACWR {acwr}")
    print()


def cmd_show(cfg: Config, args) -> int:
    con = store.connect(cfg.store)
    w = _daily_wide(con)
    if w.empty:
        print("no daily metrics: run `python -m miaband compute` first")
        return 1
    if args.date:
        if args.date not in w.index:
            print(f"{args.date} is outside the data ({w.index.min()} -> {w.index.max()})")
            return 1
        w = w.loc[:args.date]
    w = w.tail(args.days + 1)
    _summary(w.index[-1], w.iloc[-1])
    print(HRV_NOTICE)
    print()
    hdr = (f"{'date':<11} {'sleep':>6} {'bed':>5}-{'wake':<5} {'SME':>4} {'deep/light/REM':>15} "
           f"{'nap':>4} {'SRI7':>5} {'RHR':>4} {'RHR z':>6} | {'load':>5} {'strain':>6} {'ACWR':>5}  notes")
    print(hdr)
    print("-" * len(hdr))
    for date, r in w.iterrows():
        notes = []
        if r.get("sleep.tst_min:status") != "ok":
            sleep_part = f"{'—':>6} {'no main sleep recorded':<73}"
        else:
            stages = (f"{_num(r['sleep.deep_min'])}/{_num(r['sleep.light_min'])}/{_num(r['sleep.rem_min'])}"
                      if r.get("sleep.rem_min:status") == "ok" else "n/a")
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
            sme = r["sleep.sme"] * 100 if not pd.isna(r["sleep.sme"]) else np.nan
            sleep_part = (f"{_hm(r['sleep.tst_min']):>6} {_hhmm(r['sleep.onset_clock'])}-{_hhmm(r['sleep.wake_clock'])} "
                          f"{_num(sme):>3}% {stages:>15} {_num(r['sleep.nap_min']):>4} "
                          f"{_num(r.get('sleep.sri_7d')):>5} {_num(r.get('phys.rhr')):>4} {zs:>6}")
        if r.get("strain.load:status") == "ok":
            load, strain = _num(r.get("strain.load")), _num(r.get("strain.strain"), "{:.1f}")
            if isinstance(r.get("strain.load:reason"), str):
                load += "*"
        else:
            load, strain = "?", "?"
            notes.append(f"load: {r.get('strain.load:reason')}")
        acwr = _num(r.get("acwr.ewma"), "{:.2f}") if r.get("acwr.ewma:status") == "ok" else {
            "warming_up": "warm"}.get(r.get("acwr.ewma:status"), "n/a")
        if r.get("strain.unrecorded_hint") == 1:
            notes.append("high HR without a recorded workout?")
        print(f"{date:<11} {sleep_part} | {load:>5} {strain:>6} {acwr:>5}  {'; '.join(notes)}")
    # latest values that exist (tonight's RHR may be missing even when the baseline isn't)
    last = w.ffill().iloc[-1]
    print()
    print("7-day means: sleep " + _hm(last.get("tst.mean7")) + ", RHR " + _num(last.get("rhr.mean7"), "{:.1f}")
          + " | 28-day baseline: RHR " + _num(last.get("rhr.base28"), "{:.1f}")
          + ", sleep " + _hm(last.get("tst.base28")))
    print("SME = sleep maintenance efficiency (TST / sleep period); the band has no real time-in-bed.")
    print("load = Edwards TRIMP of the day's workouts; strain = 21*(1-exp(-load/tau)), a readability scale;")
    print("0* = no workout recorded, but HR too sparse to rule out an unrecorded session.")
    print("ACWR = EWMA 7d/28d load ratio: descriptive only (weak evidence as an injury predictor).")
    return 0


def cmd_sanity(cfg: Config, args) -> int:
    """Our nightly values next to the band's own, for the last N nights."""
    con = store.connect(cfg.store)
    tables = pipeline.load_tables(con)
    wide, workouts = pipeline.compute_daily(tables, cfg)
    if "tst_min" not in wide:
        print("no nights to compare: import an export first")
        return 1
    if args.date:
        wide = wide[wide["date"] <= pd.Timestamp(args.date)]
        workouts = workouts[pd.to_datetime(workouts["date"]) <= pd.Timestamp(args.date)] if not workouts.empty else workouts
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
            onset=_hhmm(n.onset_clock), wake=_hhmm(n.wake_clock),
            utc=f"UTC{n.offset_end / 60:+g}" + ("*" if n.tz_change else "")))
    df = pd.DataFrame(rows)
    print(f"{'night':<11} {'TST ours':>9} {'band':>6} {'Δmin':>5}   {'bed-wake':<11} "
          f"{'RHR ours':>8} {'band sleep min':>14} {'band RHR':>8} {'offset':>8}")
    for r in df.itertuples(index=False):
        diff = r.tst - r.dev_tst if not pd.isna(r.dev_tst) else np.nan
        print(f"{r.date:<11} {_hm(r.tst):>9} {_hm(r.dev_tst):>6} {_num(diff, '{:+.0f}'):>5}   {r.onset}-{r.wake} "
              f"{_num(r.rhr, '{:.1f}'):>8} {_num(r.dev_sleep_min_hr):>14} {_num(r.dev_rhr):>8} {r.utc:>8}")
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
    print("offset * = time-zone change vs the previous night (excluded from regularity for 2 nights)")
    print("Compare a few nights with the sleep screen in Mi Fitness, too.")
    _sanity_missing_nights(wide.tail(14), ses, cfg)
    _sanity_regularity(wide.tail(14))
    _sanity_strain(wide, workouts)
    rc = pipeline.detector_recall(tables, cfg, since="2025-01-01")
    if rc.get("n"):
        print(f"detector check: it finds {rc['found']}/{rc['n']} ({rc['found'] / rc['n']:.0%}) of your recorded "
              "workouts since 2025 when pretending they were not recorded (low recall = detected loads are a floor)")
    return 0


def _sanity_missing_nights(w: pd.DataFrame, ses: pd.DataFrame, cfg: Config) -> None:
    missing = w[w["episode"].isna()]
    if missing.empty:
        return
    print()
    print("=== Days without a main sleep (>= 3 h): sleep records ending that day ===")
    end_local = pd.to_datetime(ses["end_ts"], unit="s", utc=True).dt.tz_convert(cfg.tz)
    start_local = pd.to_datetime(ses["start_ts"], unit="s", utc=True).dt.tz_convert(cfg.tz)
    for d in missing["date"]:
        m = end_local.dt.date == pd.Timestamp(d).date()
        recs = ", ".join(f"{a:%H:%M}-{b:%H:%M} ({(b - a).total_seconds() / 60:.0f} min{', nap' if n == 1 else ''})"
                         for a, b, n in zip(start_local[m], end_local[m], ses.loc[m, "is_nap"]))
        print(f"{pd.Timestamp(d):%Y-%m-%d}: {recs or 'no sleep record at all'}")


def _sanity_regularity(w: pd.DataFrame) -> None:
    print()
    print("=== Regularity / wear, last 14 days ===")
    print(f"{'date':<11} {'worn 08-22':>10} {'nights in 7d':>12} {'SRI pairs':>9} {'SRI':>5}  why missing")
    for r in w.itertuples(index=False):
        print(f"{pd.Timestamp(r.date):%Y-%m-%d} {_num(r.day_wear * 100 if not pd.isna(r.day_wear) else np.nan):>9}% "
              f"{_num(getattr(r, 'reg_n', np.nan)):>12} {_num(getattr(r, 'sri_pairs', np.nan)):>9} "
              f"{_num(getattr(r, 'sri', np.nan)):>5}  {r.sri_reason if isinstance(getattr(r, 'sri_reason', None), str) else ''}")


ZONE_MID = np.array([0.55, 0.65, 0.75, 0.85, 0.95])


def _sanity_strain(wide: pd.DataFrame, wl: pd.DataFrame) -> None:
    from scipy.stats import spearmanr
    print()
    print("=== Training load (Edwards) ===")
    if wl.empty:
        print("no workouts")
        return
    wl = wl.assign(year=pd.to_datetime(wl["date"]).dt.year)
    by = wl.pivot_table(index="year", columns="method", values="start_ts", aggfunc="count", fill_value=0)
    print("workouts per year by method (hr_ours = our zones from >= 1/2-min HR; device_zones = band's;"
          " low_resolution = no load):")
    print(by.to_string())
    both = wl.dropna(subset=["load_ours", "load_device"])
    if len(both) >= 5:
        ratio = both["load_ours"] / both["load_device"]
        rho = spearmanr(both["load_ours"], both["load_device"]).statistic
        print(f"\nours vs band zones on {len(both)} workouts with both: median ratio {ratio.median():.2f} "
              f"(IQR {ratio.quantile(.25):.2f}-{ratio.quantile(.75):.2f}), Spearman rho {rho:.2f}")
    z = wl[[f"zone{i}_s" for i in range(1, 6)]].to_numpy(dtype=float)
    dur = (wl["end_ts"] - wl["start_ts"]).to_numpy(dtype=float)
    ok = ~np.isnan(z).any(axis=1) & (z.sum(axis=1) >= 0.9 * dur) & wl["avg_hr"].notna().to_numpy()
    if ok.sum() >= 5:
        frac = (z[ok] * ZONE_MID).sum(axis=1) / z[ok].sum(axis=1)
        implied = wl.loc[ok, "avg_hr"].to_numpy(dtype=float) / frac
        cv = implied.std() / implied.mean()
        print(f"band zones: implied HRmax = avg HR / zone midpoint = {np.median(implied):.0f} bpm "
              f"(CV {cv:.1%}, n={ok.sum()}) -> " + ("consistent with %HRmax zones" if cv < 0.05 else
                                                    "spread is large: zones may not be plain %HRmax"))
    det = wl[wl["method"] == "detected"]
    print(f"detected (not started on the band) sessions: {len(det)} in total, {len(det[pd.to_datetime(det['date']) >= pd.Timestamp('2025-01-01')])} since 2025")
    last = wide.dropna(subset=["hr_max"]).iloc[-1]
    print(f"HRmax in use today: {last['hr_max']:.0f} bpm ({last['hr_max_basis']}); strain tau = {last['tau']:.0f}")
    print("\nlast 10 workouts:")
    print(f"{'date':<11} {'sport':<18} {'min':>4} {'avgHR':>5} {'ours':>6} {'band':>6}  method  (detected = not started on the band)")
    for r in wl.tail(10).itertuples(index=False):
        print(f"{pd.Timestamp(r.date):%Y-%m-%d} {str(r.sport)[:18]:<18} {(r.end_ts - r.start_ts) / 60:>4.0f} "
              f"{_num(r.avg_hr):>5} {_num(r.load_ours):>6} {_num(r.load_device):>6}  {r.method}")


def cmd_serve(cfg: Config, args) -> int:
    try:
        import uvicorn
        from .web.app import create_app
    except ImportError:
        print('the dashboard needs the web extra: pip install -e ".[web]"', file=sys.stderr)
        return 2
    print(f"miA-band dashboard on http://{args.host}:{args.port}  (store: {cfg.store}; Ctrl+C to stop)")
    uvicorn.run(create_app(cfg.store), host=args.host, port=args.port, log_level="warning")
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
    args.date = None
    return cmd_show(Config(**{**cfg.__dict__, "store": path}), args)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="miaband", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.toml", help="TOML config (default: config.toml if present)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import-mifitness")
    p.add_argument("folder", nargs="?", default="data/mifitness",
                   help="unzipped export folder (default: data/mifitness)")
    sub.add_parser("compute")
    p = sub.add_parser("show"); p.add_argument("--days", type=int, default=7)
    p.add_argument("--date", help="as-of date YYYY-MM-DD (default: last day with data)")
    p = sub.add_parser("sanity"); p.add_argument("--days", type=int, default=30)
    p.add_argument("--date", help="as-of date YYYY-MM-DD (default: last day with data)")
    p = sub.add_parser("demo"); p.add_argument("--db", default="data/demo.sqlite"); p.add_argument("--days", type=int, default=7)
    p = sub.add_parser("serve", help="local dashboard (read-only over the computed store)")
    p.add_argument("--host", default="127.0.0.1", help="127.0.0.1 = this machine only")
    p.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    return {"import-mifitness": cmd_import_mifitness, "compute": cmd_compute, "show": cmd_show,
            "sanity": cmd_sanity, "demo": cmd_demo, "serve": cmd_serve}[args.cmd](cfg, args)
