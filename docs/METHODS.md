# Methods

How every metric is computed, and the reasoning or literature behind each choice. Proprietary
scores (WHOOP, Oura, Xiaomi) are not replicated; the methods are documented, published ones.
All thresholds named here live in `config.toml` (see `config.example.toml`).

**Contents**
1. [Pipeline](#1-pipeline)
2. [Canonical schema and ingestion](#2-canonical-schema-and-ingestion)
3. [Time](#3-time)
4. [Wear and data sufficiency](#4-wear-and-data-sufficiency)
5. [Sleep](#5-sleep)
6. [Resting heart rate](#6-resting-heart-rate)
7. [Personal baselines and flags](#7-personal-baselines-and-flags)
8. [Training load and strain](#8-training-load-and-strain)
9. [ACWR](#9-acwr)
10. [Recovery](#10-recovery)
11. [Readiness](#11-readiness)
12. [Steps](#12-steps)
13. [HRV: why there is none](#13-hrv-why-there-is-none)
14. [Testing](#14-testing)
15. [References](#references)

---

## 1. Pipeline

```
source export ─► adapter ─► canonical batch ─► SQLite raw store ─► features ─► daily metrics ─► CLI / dashboard
                 (Mi Fitness,                  (idempotent        (pure functions
                  Gadgetbridge,                 ingest)            over DataFrames)
                  synthetic)
```

- Ingestion is incremental. Features are a **full recompute**: a few seconds for ~10 years of
  minute-level data, and a config change (weights, HRmax) applies consistently to all history.
- Every daily value is stored with a `status` (`ok`, `insufficient_data`, `not_available`,
  `warming_up`) and a human-readable `reason`. **Nothing is imputed**: a missing night is shown
  as missing, with the reason.
- Dependencies: pandas, numpy, scipy for analytics; FastAPI + uvicorn only for the dashboard.
  No network calls; Chart.js is vendored.

## 2. Canonical schema and ingestion

All instants are UTC epoch seconds. Every row carries `source` and `device_id`, so several
devices and sources can coexist.

| table | content |
|---|---|
| `hr_samples` | `ts, bpm, context` (`background` / `workout` / `spot`) |
| `step_samples` | steps per active minute |
| `sleep_sessions` | envelope, optional in-bed times, recorded UTC offset, device-reported totals (for cross-checks only) |
| `sleep_segments` | `start_ts, end_ts, stage` ∈ {awake, light, deep, rem, asleep} |
| `workouts` | start/end, sport, recorded offset, avg/max HR, device zone durations |
| `rr_intervals` | defined; empty for the current sources |
| `device_daily` | device-computed daily values (e.g. resting HR), reference only |

- **Idempotency.**
  - Point tables are upserted on their natural key; a later export wins.
  - Interval tables are **replaced by window**: rows of the same source overlapping the batch's
    time span are deleted and re-inserted in one transaction. A night the device re-segmented
    between exports leaves no orphans.
  - Re-importing an overlapping export is a no-op.
- **Adapters fail loudly.** An unknown sleep-stage code aborts the import rather than being
  guessed. Stage mappings are verified against the device's own per-stage totals on every import
  (see `docs/MIFITNESS_FORMAT.md`).
- **Several sources on one day:** features use the highest-priority source for that day and
  never mix sources within a day (`[sources] priority`).

## 3. Time

- Sleep and workout records carry the **UTC offset in force at the time**. Every other sample
  takes the offset of the nearest such record (within 36 h); otherwise the configured fallback
  zone applies, with its DST rules. This handles travel and moving between zones without
  configuration.
- A record whose offset equals the home zone's offset uses the home zone's DST-aware offset at
  each endpoint, so a night that spans a DST switch gets the right wall-clock wake time.
- Devices that store the standard offset all year (never changing at EU DST switches, checked
  over ≥ 3 switches) are detected per device. Their UTC+0/+1 offsets are corrected inside EU
  summer time.
- Local days are 23, 24 or 25 h long; every coverage ratio uses the actual day length.
- An offset change between nights that is not a DST switch is flagged `tz_change`; the next two
  nights are excluded from regularity metrics (jet lag is not irregular habit).

## 4. Wear and data sufficiency

- **Worn intervals** come from HR continuity. Consecutive valid samples (25–230 bpm) closer than
  `max(3 × local cadence, 10 min)` are joined. Cadence is a rolling median of sample gaps, so 1-min
  and 10-min devices need no configuration.

| unit | valid when |
|---|---|
| night (sleep physiology) | HR covers ≥ 80 % of the sleep period |
| workout-free day = load 0 | band worn ≥ 70 % of 08:00–22:00 local; otherwise unknown |
| day's steps = 0 when no step rows | same wear rule; otherwise unknown |

## 5. Sleep

**Episodes.**
- Sessions less than 60 min apart merge into one episode.
- Overlapping sessions from one source are duplicates; the longer one is kept.
- The **wake date** is the local date of the episode's end.
- The **main sleep** is the longest non-nap episode of at least 3 h; every other episode is a nap.

**Per-night metrics.**
- **TST:** minutes of non-awake segments.
- **SPT:** sleep onset to final awakening.
- **WASO:** SPT − TST.
- **SME = TST / SPT**, sleep maintenance efficiency. Classic sleep efficiency (TST / time in bed)
  is *not* reported: the wrist device's "in bed" period is an algorithmic envelope (onset minus a
  fixed few minutes), not a measured bed entry.
- **Stages** are shown only on nights where the device produced REM. Older devices had no REM
  detection, so their "light" also contains REM. Consumer wrist staging agrees only moderately
  with polysomnography [de Zambotti 2019; Chinoy 2021], so stages are labelled device estimates.

**Regularity**, over the 7 nights ending at each date:
- **Sleep Regularity Index** [Phillips 2017].
  - Built on a per-minute local wall-clock sleep/wake grid.
  - SRI = 200 · P(same state at the same minute on consecutive days) − 100.
  - Minutes with unknown state (not worn, not asleep) are excluded.
  - A day pair counts if ≥ 50 % of its minutes are known on both days; at least 5 pairs are
    required.
- **Circular SD** of onset and wake clock times, so 23:50 vs 00:10 is a 20-min difference.

## 6. Resting heart rate

- **Nightly RHR** = the lowest rolling *mean* HR over a window W inside the main sleep, skipping
  the first 30 min.
  - W = max(10 min, 3 × cadence).
  - A window counts if it holds ≥ 70 % of the samples expected at that cadence.
- The nocturnal nadir is the least confounded RHR a wrist device can give: no posture, activity
  or caffeine effects [Buchheit 2014]. The rolling mean protects against single-sample optical
  artifacts.
- Mean sleeping HR is reported alongside.
- It is always lower than a seated morning RHR or a device's daytime "resting HR".

## 7. Personal baselines and flags

For each nightly metric (RHR, TST, mean sleeping HR):
- **28-day baseline:** median and spread = max(1.4826 · MAD, floor), over valid values from the
  28 days *before* the date, on the **same device** (a sensor change shifts values and must not
  read as a deviation).
  - It needs ≥ 14 valid values; until then the status is `warming_up`.
  - Robust statistics, because n is small and one sick night must not inflate the spread.
  - The floors are 1 bpm for RHR and 20 min for TST.
- **7-day mean** (needs ≥ 4 values), shown as a trend.
- **z** = (value − median) / spread. A night is **flagged** when |z| ≥ 1.5 in the unfavourable
  direction: RHR up, sleep down.

## 8. Training load and strain

**Edwards TRIMP** [Edwards 1993] per session = Σ minutes in five %HRmax zones (50–60, 60–70,
70–80, 80–90, 90–100 %) × weights 1–5.
- **Zone minutes are computed by the app** from the HR samples inside the session window, so one
  definition applies to every device and year.
  - It needs HR at ≤ 2-min cadence covering ≥ 80 % of the session.
  - Each sample holds until the next one, capped at 2 × cadence so gaps are not credited.
- **Fallback:** the device's own zone durations, labelled as such. The app checks they are %HRmax
  zones by fitting the HRmax they imply (average HR ÷ time-weighted zone midpoint) and reporting
  its spread.
- Otherwise the session's load is **unknown**, and so is its day. Nothing is estimated from
  average HR.

**Sessions not started on the device** are detected in dense background HR.
- Rule: a 5-sample rolling median ≥ 60 % HRmax for ≥ 20 min, outside recorded sessions (±5 min).
  Stretches less than 10 min apart merge.
- Detected sessions get the same Edwards load.
- `miaband sanity` reports the detector's recall by running it on recorded sessions as if they
  had not been recorded. Detected loads are a floor.

**HRmax** per date is the higher of:
- 220 − age at that date (age is tracked over the years);
- the median of the 3 highest *sustained* session peaks (3-sample rolling median) in the past
  24 months, ignoring values above 220 − age + 15 as artifacts. Single-sample device maxima are
  not used, because optical spikes during arm-heavy sport would inflate HRmax.

**Daily load** = Σ session loads. A workout-free day is 0 only when the band was worn (§4).

**Secondary: Banister TRIMP** [Banister 1991].
- Formula: Σ Δt · HRr · a · e^(b·HRr), over all samples at ≥ 30 % heart-rate reserve, on days
  with ≤ 2-min HR.
- HRrest is the trailing nightly-RHR baseline.
- It is used only to hint at unrecorded sessions ("high HR without a recorded workout?").

**Strain (0–21)** = 21 · (1 − e^(−load/τ)).
- τ is calibrated so that the median training day of the most recent 365 days scores 12.
- It is a readability transform, not physiology, and not WHOOP's proprietary strain; it only
  uses the same range.

## 9. ACWR

- Acute:chronic workload ratio with **exponentially weighted moving averages** [Williams 2017]:
  acute 7 d, chronic 28 d, λ = 2/(N+1), uncoupled. The rolling 7/28-day average ratio is computed
  alongside.
- A day with unknown load leaves both averages unchanged; it is not imputed as 0, and is counted.
- The ratio is `insufficient_data` when:
  - more than 1 day is unknown in the last 7;
  - more than 4 days are unknown in the last 28;
  - there are fewer than 28 days of history;
  - chronic load is close to zero.
- **Limitations, stated in the UI as well:**
  - it is a ratio of correlated quantities;
  - it is unstable at low chronic load;
  - the evidence linking it to injury risk is weak and contested [Impellizzeri 2020].
- It is presented as a descriptive load-change indicator. The 0.8–1.3 band [Gabbett 2016] is a
  reference, not a prediction.

## 10. Recovery

Without HRV, recovery uses two components, both against the 28-day baseline and signed so that
positive = better, each clipped to [−3, 3]:

| component | z | default weight |
|---|---|---|
| resting HR | −z(RHR) | 0.5 |
| sleep | +z(TST) | 0.5 |

- Z = Σ wᵢ zᵢ / Σ wᵢ.
- **Score = 100 · Φ(Z / σ)**, with σ = √(Σ wᵢ²) / Σ wᵢ.
  - Under independence this is the percentile of today against the personal baseline: 50 is a
    typical day, 84 is about +1 SD.
  - The components correlate somewhat, so it reads as approximately a percentile.
- **Exact decomposition**, stored per day: pointsᵢ = (score − 50) · wᵢzᵢ / Σ wⱼzⱼ, so
  score = 50 + Σ pointsᵢ.
- A missing component or a baseline still warming up gives no score; the score is never
  renormalised onto fewer inputs.
- The weights are a judgment call, not literature values, which is why they are configurable
  and every score can be decomposed.
- RHR elevation relative to baseline as a fatigue/illness marker [Buchheit 2014]; sleep
  restriction impairs recovery and performance.

## 11. Readiness

An ordered list of `{field, op, value}` rules from config; the first rule whose terms all hold
wins.
- A small explicit evaluator is used, not `eval`.
- A term on a missing value is false, so ACWR rules stay silent while ACWR is unavailable.
- The output records which rule fired.

The default rules:
1. insufficient data;
2. RHR z ≥ 2 after a short night → possible illness or fatigue;
3. recovery < 34 → recovery day;
4. ACWR > 1.5 → load spike;
5. recovery ≥ 67 and ACWR < 0.8 → room to push;
6. otherwise → train as planned.

## 12. Steps

- Devices write one row per active minute, so a worn day without rows is 0, and a day without
  wear is unknown.
- When devices overlap (a switch day, a phone also counting), a day's steps are the **busiest
  device's** total, never the sum.

## 13. HRV: why there is none

RMSSD needs beat-to-beat (RR) intervals; per-minute average HR cannot produce it. The current
sources export neither RR intervals nor a device HRV value, so no HRV is computed and nothing else
is labelled HRV.

The canonical schema already has an `rr_intervals` table. The method is **planned, not yet
implemented**, for when a source provides RR data (e.g. Gadgetbridge with a device that records
it):
- filtering: physiological range 300–2000 ms, then a local-median successive-difference filter
  [Lipponen 2019];
- 5-min windows [Task Force 1996];
- a nightly median of ln(RMSSD), trended over 7 days [Plews 2012].

## 14. Testing

Feature tests run on a **scenario generator** that writes the canonical schema with planted
events and known expected outputs. Among the events covered:
- short nights, awake bouts, naps, missing nights;
- illness nights (+bpm);
- non-wear, sparse 10-min sampling, single-sample artifacts;
- both DST switches;
- travel across zones;
- device changes;
- unrecorded sessions.

The Mi Fitness adapter is tested by a round trip: the generator writes the export's CSV layout,
and the adapter must reproduce the canonical tables and the same nightly metrics.

## References

- Banister EW (1991). Modeling elite athletic performance. In *Physiological Testing of
  Elite Athletes*.
- Buchheit M (2014). Monitoring training status with HR measures. *Front Physiol* 5:73.
- Chinoy ED et al. (2021). Performance of seven consumer sleep-tracking devices compared with
  polysomnography. *Sleep* 44(5).
- de Zambotti M et al. (2019). Wearable sleep technology in clinical and research settings.
  *Med Sci Sports Exerc* 51(7).
- Edwards S (1993). *The Heart Rate Monitor Book*.
- Gabbett TJ (2016). The training–injury prevention paradox. *Br J Sports Med* 50(5).
- Impellizzeri FM et al. (2020). Acute:chronic workload ratio: conceptual issues and fundamental
  pitfalls. *Int J Sports Physiol Perform* 15(6).
- Lipponen JA, Tarvainen MP (2019). A robust algorithm for heart rate variability time series
  artefact correction. *J Med Eng Technol* 43(3).
- Phillips AJK et al. (2017). Irregular sleep/wake patterns are associated with poorer academic
  performance and delayed circadian and sleep/wake timing. *Sci Rep* 7:3216.
- Plews DJ et al. (2012). Heart rate variability in elite triathletes. *Eur J Appl Physiol* 112.
- Task Force of the ESC and NASPE (1996). Heart rate variability: standards of measurement.
  *Circulation* 93(5).
- Tanaka H et al. (2001). Age-predicted maximal heart rate revisited. *J Am Coll Cardiol* 37(1).
- Williams S et al. (2017). Better way to determine the acute:chronic workload ratio?
  *Br J Sports Med* 51(3).
