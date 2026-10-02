# miA-band
Private alternative to proprietary mi band app

Local, offline health analytics for a Xiaomi Smart Band 10 synced via Gadgetbridge.

## Step 0: inspect your Gadgetbridge export

```sh
adb pull /sdcard/Android/data/nodomain.freeyourgadget.gadgetbridge/files/Gadgetbridge ./data/Gadgetbridge
python scripts/inspect_gadgetbridge.py data/Gadgetbridge --tz Europe/Rome --out inspect_out
```

Writes `inspect_out/report.txt`, `schema.sql` and `samples.txt`. Stdlib only, opens the DB read-only.
MAC addresses and the user name are redacted unless you pass `--no-redact`.
`data/` and `inspect_out/` are git-ignored: health data never gets committed.

## Inspect a Mi Fitness export

Unzip the export into `data/mifitness/` (git-ignored), then:

```sh
python scripts/inspect_mifitness.py data/mifitness --out inspect_mifitness_out
```

This writes `report.txt` and `samples.txt`. For every Key in the key/value files, the report covers the JSON payload structure, time range, device ids, sampling cadence and duplicate timestamps. The account id, user and device ids, and all GPS values are masked unless you pass `--no-redact`.

## Setup

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp config.example.toml config.toml        # optional; defaults match it
python -m pytest                          # synthetic-data tests
```

## Step 2: import, compute, show

```sh
python -m miaband import-mifitness data/mifitness   # idempotent: re-run on every new export
python -m miaband compute                           # full recompute of nightly metrics
python -m miaband show --days 7                     # last 7 days + today's recovery/readiness
python -m miaband show --date 2026-07-15            # same, as of any past day
python -m miaband sanity --days 30                  # our numbers next to the band's own
python -m miaband demo                              # try it on synthetic data (data/demo.sqlite)
```

What each metric means, and why it's computed the way it is: `docs/SPEC.md`.

## Step 5: dashboard

```sh
pip install -e ".[web]"
python -m miaband serve            # http://127.0.0.1:8765  (e.g. ?date=2026-07-20&days=90)
```

Read-only over the computed store: run `compute` after each import, then reload the page.
Chart.js is vendored in `miaband/web/static/` (MIT licence), so the page makes no external requests.
