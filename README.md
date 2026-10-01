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
