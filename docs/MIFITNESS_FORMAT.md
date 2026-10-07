# Mi Fitness export format (as observed)

Xiaomi doesn't document the Mi Fitness data export. This is what was established by profiling
real exports with `scripts/inspect_mifitness.py`. That script redacts account and device ids
and never prints GPS values, so its output can be shared safely. Treat everything here as
observed behaviour, not a specification. The adapter verifies the critical parts on every import.

## Files

The export is a zip of `<date>_<account>_MiFitness_<table>.csv` files. Two of them matter:

| file | layout | use |
|---|---|---|
| `hlth_center_fitness_data.csv` | `Uid, Sid, Key, Time, Value(JSON), UpdateTime`, one row per (key, time) | HR, steps, sleep, device daily values |
| `hlth_center_sport_record.csv` | `Uid, Sid, Key(sport), Time, Category, Value(JSON), UpdateTime` | one row per workout: summary only, no per-second HR |

- `user_fitness_data_records.csv` is the legacy (Mi Fit / Zepp) store and largely duplicates the
  main file. One part might be added later: per-day base64 minute-HR blobs (`watch_hrm_record.raw_hrm`)
  that could fill early HR gaps.
- `Sid` identifies the data source or device. Data imported from the older Mi Fit / Zepp app can
  put several physical bands under one `Sid`, so device changes inside that period are invisible.

## Keys used

| key | JSON | canonical table |
|---|---|---|
| `heart_rate` | `{time, bpm, type}` | `hr_samples` (background) |
| `single_heart_rate` | `{time, bpm}` | `hr_samples` (spot) |
| `steps` | `{time, steps, distance, calories}`, per active minute | `step_samples` |
| `watch_night_sleep` (older), `sleep` (newer) | `bedtime, wake_up_time, duration, sleep_{deep,light,rem,awake}_duration, timezone, items[]{start_time, end_time, state}`; newer adds `bed_timestamp, out_bed_timestamp, avg_hr, min_hr, max_hr` | `sleep_sessions` + `sleep_segments` |
| `watch_daytime_sleep` | `items[]` (no envelope) | naps |
| `resting_heart_rate` | `{date_time, bpm}`; `date_time` is 00:00 UTC of the local date | `device_daily` (reference only) |
| sport records | `start_time, end_time, timezone, avg_hrm, max_hrm, min_hrm, hrm_{warm_up,fat_burning,aerobic,anaerobic,extreme}_duration` (s), `train_load` | `workouts` |

These keys are present but unused: `calories`, `intensity` (timestamp only), `stress`, `pai`,
`vitality`, `training_load`, `vo2_max`, `valid_stand`, `spo2`. `body_momentum` and
`light_sensitivity_value` were all zeros.

## Sleep stage codes

| code | stage | evidence |
|---|---|---|
| 2 | deep | items sum to `sleep_deep_duration` on ~100 % of nights |
| 3 | light | items sum to `sleep_light_duration` on ~100 % of nights |
| 4 | REM | items sum to `sleep_rem_duration` on all nights that have REM |
| 5 | awake | items sum to `sleep_awake_duration` on all nights that have awake time |
| 0 | asleep, unclassified | early records only: the first hour of the night |

The adapter recomputes these sums on every import and prints the agreement rate per code. It
aborts on an unknown code, or if deep/light agreement drops below 95 %.

- Some devices produce no REM at all. Their "light" then includes REM, so stage breakdowns are
  shown only on nights with REM.
- In the newer records, the in-bed period starts a fixed few minutes before sleep onset and ends
  at wake. It is not a measured bed entry, so sleep efficiency isn't computed from it.

## Time

- `timezone` on sleep and workout records is the UTC offset in **15-minute units** (e.g. 4 = UTC+1,
  8 = UTC+2).
- Records from the older app carry the *standard* offset all year, with no DST. The adapter
  detects this per device and corrects it (see `docs/METHODS.md` §3).
- `Time` and the JSON timestamps are UTC epoch seconds.
- Rows dated 2000-12-31 appear when a device's clock was unset. Anything before 2015 or in the
  future is dropped.

## Not in the export

- **No RR intervals and no HRV value** of any kind.
- **No per-second workout HR.** Workout load is computed from background HR during the workout,
  or from the band's zone durations.
- Background HR cadence follows the band's "heart rate monitoring" setting; 1 or 10 minutes were
  both observed. At 10 minutes, resting HR uses a wider window and per-workout zones fall back
  to the band's own.
