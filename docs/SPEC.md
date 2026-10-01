# miA-band: Step 1 spec (source-agnostic)

Status: **draft, awaiting approval**. Everything below is defined against a canonical internal
schema. Source adapters (Mi Fitness CSV export, then Gadgetbridge) only have to produce that
schema. Items that depend on real data are marked **[TBD-data]** and collected in §9.

User parameters: male, age 29, 179 cm / 79 kg (not used by any current metric), max HR unknown,
resting HR ≈ 60–70, Europe/Madrid, beach volleyball 3–4×/week with a workout started on the band. Runs on Windows/WSL and a Linux home server. Target Python ≥ 3.11
(for `tomllib` and `zoneinfo`).

---

## 1. Architecture

```
sources/<adapter> ──► CanonicalBatch ──► ingest (idempotent upsert) ──► raw store (SQLite)
                                                                          │
               config.toml ──► features (pure fns over DataFrames) ◄──────┘
                                         │
                                         ▼
                                 daily store (SQLite) ──► CLI  (later: FastAPI + Chart.js)
```

```
miaband/
  config.py            load + validate config.toml (tomllib), typed dataclasses
  canonical.py         canonical table definitions, dtypes, validators
  sources/
    base.py            Adapter protocol: read(path) -> CanonicalBatch
    synthetic.py       scenario-driven generator (Step 2a)
    mifitness.py       Mi Fitness CSV export adapter (after the export arrives)
    gadgetbridge.py    Gadgetbridge SQLite adapter (later)
  store.py             SQLite schema, upsert/replace strategies, reads
  ingest.py            batch -> store, ingest_log
  features/
    timeutil.py        local-day boundaries, DST-aware day lengths
    wear.py            worn intervals and per-day coverage
    nights.py          sleep episode assembly, wake-date assignment
    sleep.py           TST, efficiency/WASO, stages, regularity
    physiology.py      nightly resting HR, HRV (if RR data exists)
    baselines.py       trailing robust baselines, z-scores, flags
    strain.py          HRmax/HRrest resolution, TRIMP, daily strain, ACWR
    recovery.py        component z-scores, weighted score, decomposition
    readiness.py       rule engine
  pipeline.py          recompute daily metrics from the raw store
  cli.py               `miaband ingest|compute|show`
scripts/inspect_gadgetbridge.py   (exists, kept for later)
tests/                 pytest, fixtures from the synthetic generator
config.example.toml
```

Decisions:

- **SQLite for both stores** (one file, `data/miaband.sqlite`): stdlib, readable on every machine
  you use, and enough for this volume (~0.5–1 M HR rows/year). DuckDB would add a dependency for
  no real gain at this size.
- **Ingestion is incremental, features are a full recompute.** Ingest only appends or replaces
  what's new. `compute` rebuilds every daily metric from the raw tables, which takes seconds at
  this volume. That keeps features pure and deterministic, and a config change (weights, HRmax)
  applies to history consistently. If it gets slow, it can be narrowed later to "earliest changed
  day − 60 days".
- Dependencies: pandas, numpy, scipy (scipy is used for the normal CDF). Tests use pytest. No
  network calls.

## 2. Canonical schema

All instants are stored as **UTC epoch integers**: seconds, or milliseconds for RR. Local time is
only derived inside features, from `config.timezone`. Every table carries `source` (`mifitness`,
`gadgetbridge`, `synthetic`) and `device_id`, so the two sources can coexist.

| table | columns | key | required? |
|---|---|---|---|
| `hr_samples` | `source, device_id, ts, bpm, context` | `(source, device_id, ts)` | yes |
| `sleep_sessions` | `source, device_id, start_ts, end_ts, in_bed_start_ts?, in_bed_end_ts?, is_nap?` | `(source, device_id, start_ts)` | yes |
| `sleep_segments` | `source, device_id, start_ts, end_ts, stage` | `(source, device_id, start_ts)` | optional |
| `rr_intervals` | `source, device_id, ts_ms, rr_ms` | `(source, device_id, ts_ms)` | optional |
| `device_hrv` | `source, device_id, ts, value, method` | `(source, device_id, ts, method)` | optional |
| `wear_intervals` | `source, device_id, start_ts, end_ts, worn` | `(source, device_id, start_ts)` | optional |
| `step_samples` | `source, device_id, ts, steps` | `(source, device_id, ts)` | optional (used for wear inference) |
| `workouts` | `source, device_id, start_ts, end_ts, sport` | `(source, device_id, start_ts)` | optional (labels only) |
| `ingest_log` | `batch_id, source, file_sha256, ingested_at, table, rows, min_ts, max_ts` | `batch_id, table` | — |

Enums:

- `context`: `background | workout | spot | unknown`. Workout HR is usually much denser, and
  that matters for strain (§6).
- `stage`: `awake | light | deep | rem | asleep` (`asleep` = asleep, stage not given).
  Adapters map source codes to these through an **explicit table**. An unmapped code makes the
  ingest **fail** rather than get guessed at.
- `device_hrv.method`: `rmssd | sdnn | proprietary | unknown`. Only `rmssd` with a documented
  window may ever be labelled HRV next to our own value (§5.2).

Adapter contract: the adapter removes source sentinels (e.g. `bpm = 0/255`), converts to UTC, and
maps codes. It does **not** apply physiological filtering. That belongs in features, so it's
tested once for every source.

### 2.1 Idempotency

Each table has an ingest strategy:

- **Point tables** (`hr_samples`, `rr_intervals`, `step_samples`, `device_hrv`):
  `INSERT … ON CONFLICT(key) DO UPDATE`. Re-importing an overlapping export is a no-op, and if a
  later export revised a value, the later export wins.
- **Interval tables** (`sleep_sessions`, `sleep_segments`, `wear_intervals`, `workouts`):
  **replace-by-window**. For each source, delete existing rows that overlap the batch's
  `[min start_ts, max end_ts]` for that table, then insert the batch's rows in the same
  transaction. This handles a device that re-segments a night between exports, where the keys
  themselves change and an upsert would leave orphans.
- Every batch is recorded in `ingest_log` with the file hash. The same file re-ingested is
  detected and reported, though not refused.

### 2.2 Two sources, one timeline

When sources overlap (the Mi Fitness history around the switch to Gadgetbridge), features read a
**resolved view**: for each table and **local day**, use the highest-priority source that has
data for that day (`config.sources.priority`, default `["gadgetbridge", "mifitness"]`). Data from
different sources is never mixed within a day, because cadence and sentinel semantics differ.

### 2.3 Time

- Local day D = `[D 00:00, D+1 00:00)` in Europe/Madrid, computed with `zoneinfo`. It lasts 23 h
  on the last Sunday of March and 25 h on the last Sunday of October. Any "per day" coverage
  ratio divides by the **actual** day length.
- Clock times (bedtime, wake time) are expressed in **local wall-clock** time. A bedtime that stays
  at 23:30 on the clock across a DST change is therefore *not* counted as irregular.
- Single fixed time zone in v1. Travel across time zones is a known limitation; a `tz_history`
  table can come later if you need it.

## 3. Wear and data sufficiency

Unless the source gives `wear_intervals`, wear is inferred from HR continuity:

- c = the source's background sampling cadence, estimated per day as the median gap between
  consecutive `background` HR samples **[TBD-data: Mi Band background HR is configurable
  (1/5/10/30 min or "smart"); we'll see what's in the export]**.
- Consecutive valid samples (25 ≤ bpm ≤ 230) at most `gap_max = max(3·c, 10 min)` apart are
  joined into a worn interval. A longer gap counts as non-wear (or band off/charging).
- `coverage(D)` = worn minutes / minutes in D (1380, 1440 or 1500).

Sufficiency rules (all in config):

| unit | valid when | otherwise |
|---|---|---|
| strain day | `coverage ≥ 0.80` **and** no non-wear gap overlaps a recorded workout | `insufficient_data` |
| night | main sleep found, and HR coverage within the sleep episode ≥ 0.80 | `insufficient_data` |
| HRV night | ≥ 6 valid 5-min RR windows (§5.2) | `not_available` / `insufficient_data` |

Nothing is ever imputed. Each daily metric is stored with
`status ∈ {ok, insufficient_data, not_available, warming_up}` plus a human-readable `reason`.

## 4. Sleep

### 4.1 Episodes and night assignment

1. Take `sleep_sessions` and merge sessions from the same source that are < 60 min apart into
   **episodes** (a brief wake shouldn't split a night).
2. The episode's **wake date** = local date of its `end_ts`.
3. **Main sleep** for date D = the longest episode with wake date D and duration ≥ 3 h. Other
   episodes with wake date D are **naps**: reported, but excluded from main-sleep metrics and
   regularity.
4. No qualifying episode means the night is `insufficient_data` (whether you didn't sleep or
   didn't wear the band; we can't tell which).

**[TBD-data]**: whether the source already splits naps from the night, and whether a night that
crosses midnight comes as one record or two.

### 4.2 Metrics (per main sleep)

- **Total sleep time (TST)** = Σ duration of segments with stage ≠ `awake`. Without segments, it's
  session duration minus any awake time the source reports.
- **Sleep period time (SPT)** = sleep onset → final awakening (session envelope).
- **WASO** = SPT − TST.
- **Efficiency.** True sleep efficiency = TST / time in bed needs a *bed-entry/exit* signal, and
  wrist devices usually only report onset/offset. So:
  - if `in_bed_*` exists and differs from onset/offset in a meaningful share of nights → report
    **sleep efficiency** = TST / TIB;
  - otherwise → report **sleep maintenance efficiency (SME)** = TST / SPT, *labelled as such*
    (the standard actigraphy measure when there is no TIB), plus WASO. It is **never** called
    "sleep efficiency". **[TBD-data]**
- **Stages** (minutes and % of TST) **only if** segments carry real light/deep/REM codes. They are
  labelled "device estimate". Wrist-based staging agrees only moderately with PSG, especially for
  deep/REM (de Zambotti et al. 2019; Chinoy et al. 2021). **[TBD-data: stage codes]**
- Sleep onset, wake time, and mid-sleep, in local clock time.

### 4.3 Regularity (rolling 7 days, needs ≥ 5 valid nights out of 7)

- **Sleep Regularity Index (SRI)** (Phillips et al. 2017) — primary.
  - Build a per-minute local-clock sleep/wake vector from main sleep and naps.
  - SRI = 200·P(same state at the same local clock minute on consecutive days) − 100. The range
    is −100…100; 100 means perfectly regular.
  - Minute pairs where either day lacks wear coverage are excluded. The minutes that don't exist
    or exist twice on DST days are excluded as well.
  - SRI has the strongest outcome evidence among regularity metrics (e.g. Windred et al. 2024).
- **Secondary:** circular SD of sleep onset and of wake time, in minutes. Onset is treated as an
  angle on the 24 h clock (circular SD = √(−2 ln R)), so 23:50 vs 00:10 is a 20-min difference,
  not 23 h 40 min.

## 5. Nightly physiology

### 5.1 Resting HR (RHR)

- **Definition:** the lowest rolling **mean** HR over a time-based window W inside the main sleep
  episode, excluding its first 30 min (sleep-onset transients).
  - W = `max(10 min, 3·c)`, so a window holds at least ~3 samples at sparse cadences.
  - A window counts only if it holds ≥ 70 % of the samples expected at cadence c.
- **Why:** the nocturnal nadir is the least confounded RHR available from a wrist device (no
  posture, activity or caffeine from the day). A rolling mean instead of the single minimum
  protects against one-sample optical artifacts (Buchheit 2014 on HR monitoring).
- Also reported: **mean sleeping HR** (whole episode).
- **Not used:** the raw single-sample minimum (artifact-prone).
- Note: sleeping RHR runs a few bpm below the seated morning RHR you quoted (60–70). That's
  expected.

### 5.2 HRV (hard constraint)

Only computed from `rr_intervals`. **[TBD-data: whether either source has RR at all.]**

Pipeline per night:

1. **Range filter:** 300 ≤ RR ≤ 2000 ms (30–200 bpm).
2. **Successive-difference filter:** reject an RR that deviates > 20 % from the median of the
   5 surrounding accepted beats. This is a local-median criterion in the spirit of
   Lipponen & Tarvainen 2019 / Kubios.
   - A timestamp gap > 2 s between beats marks a **discontinuity**: successive differences are
     never computed across a rejected beat or a gap.
3. **Windows:** split the main sleep (minus the first 30 min) into consecutive 5-min windows (the
   standard short-term length, Task Force 1996). A window is valid if ≥ 80 % of its beats survive
   filtering and it has ≥ 200 beats.
4. **Nightly value:** RMSSD = √mean(ΔRR²) per valid window. Night HRV = **median of the windows'
   ln(RMSSD)**, requiring ≥ 6 valid windows.
   - Same rule every night = a consistent window.
   - If the device only records RR in fixed short bursts, step 3 adapts to "all valid bursts"
     with the same quality gates.
5. **Trend:** 7-day rolling mean of ln(RMSSD) against the 28-day baseline. "Smallest worthwhile
   change" = 0.5 × baseline SD (Plews et al. 2012, 2013).

**Device-computed HRV** (`device_hrv`), if it exists:

- stored, and shown **separately** as "device-reported HRV (method: …)";
- **not** used in recovery unless `method = rmssd` and its window is documented
  (`config.recovery.use_device_hrv`, default `false`);
- never merged with or relabelled as our RMSSD.

**If no RR data exists:**

- the CLI states once per run: "HRV: not available from this source (no beat-to-beat data)";
- recovery runs on the HR + sleep components only, and is titled **"Recovery (HR + sleep, no
  HRV)"**;
- no proxy is computed.

### 5.3 Baselines, z-scores, flags

For every nightly metric x (RHR, ln RMSSD, TST, mean sleeping HR):

- **28-day baseline.** The metric's valid values from the 28 local days **before** D (D excluded).
  - Center = median.
  - Spread = `max(1.4826·MAD, floor)`.
  - Needs ≥ 14 valid values, otherwise `warming_up`.
  - Robust stats because n is small and a single sick night shouldn't inflate the SD.
  - The floors (RHR 1.0 bpm, ln RMSSD 0.05, TST 20 min) stop very stable periods from producing
    huge z.
- **7-day baseline:** mean of the 7 nights before D. Needs ≥ 4 valid; shown as a trend line.
- **z(D)** = (x(D) − center₂₈) / spread₂₈.
- **Flag** when the z is in the *unfavourable* direction and |z| ≥ `flag_sd` (default 1.5):
  - RHR ↑
  - ln RMSSD ↓
  - TST ↓
- Favourable deviations are shown but never flagged.

**Warm-up:** recovery needs 14 valid nights within the last 28 days (≈ 2–3 weeks). ACWR needs
28 days with ≥ 24 valid strain days. Until then, the metric is shown as `warming_up (n/14)`.

## 6. Strain

### 6.1 HR anchors

- **HRmax**:
  - `config.hr_max` if set; otherwise `max(220 − age, observed reliable max)` = max(191, observed).
  - "Observed reliable max" = the highest 60-s rolling **median** of HR, only from windows with ≥ 3
    samples, in `workout` context or dense HR. A sustained value, not a spike: optical HR during
    volleyball (arm swings, jumps) is exactly where cadence-lock artifacts happen.
  - The auto value only moves **up**, and is logged with its date. Because features recompute in
    full, history is rescored with the current HRmax (consistent, but past strain values can shift
    a little when HRmax rises).
  - (Tanaka 208 − 0.7·age = 188 is the better-validated population formula. I'm keeping 220 − age
    as the floor as you specified; it only matters until a real max is observed.)
- **HRrest** for heart-rate reserve = the 28-day baseline center of nightly RHR; `config.resting_hr`
  (default 65) until the baseline exists.
  - Because this is a sleeping RHR, %HRR runs slightly higher than with a seated RHR. That's
    consistent over time, which is what matters for load trends.

### 6.2 Training load: Banister TRIMP (primary)

- HRr(t) = clip((HR − HRrest) / (HRmax − HRrest), 0, 1).
- TRIMP = Σ Δtᵢ[min] · HRrᵢ · a · e^{b·HRrᵢ}
  - a = 0.64, b = 1.92 (Banister 1991, male coefficients; the female set is 0.86/1.67).
- Only samples with HRr ≥ 0.30 (`strain.hrr_floor`) count, so 24 h of background HR from sitting
  and walking doesn't pile up as "training".
- Δtᵢ = min(gap to the next sample, `gap_max`). Sparse sampling can't be stretched across
  non-wear.
- **Why Banister rather than Edwards:** Banister works on HRR natively and is continuous (no zone
  cliffs). Edwards' zones are defined on %HRmax, and moving them onto %HRR would be our own
  variant, not Edwards. Time in five %HRR zones (50–60 / 60–70 / 70–80 / 80–90 / 90–100) is still
  reported for display.
- **Resolution caveat:** at 1-min background cadence a 2-h volleyball session gets ~120 samples,
  which is usable. At 10-min cadence it gets ~12, which isn't.
  - A day whose high-HR periods have c > 2 min gets `strain_quality = low_resolution`. It is
    shown, but excluded from ACWR. **[TBD-data]**

### 6.3 Daily strain scale

- Strain(D) = 21 · (1 − e^{−TRIMP(D)/τ}), with τ = 120 by default (config).
  - TRIMP 60 → 8.3, 150 → 15.0, 300 → 19.3.
  - This is a **readability transform** (saturating, so a monster day doesn't break the scale). It
    isn't physiology, and it's **not** WHOOP's strain, only the same 0–21 range.
- ACWR and anything quantitative use raw TRIMP.
- τ can be recalibrated after ~4 weeks, e.g. to put your typical session at ~12.

### 6.4 ACWR

- **Variant:** EWMA (Williams et al. 2017) on daily TRIMP, uncoupled.
  - acute λ = 2/(7+1), chronic λ = 2/(28+1); ACWR = EWMA₇ / EWMA₂₈.
  - Chosen over rolling 7:28 averages because it weights recent days progressively, so a load from
    27 days ago doesn't fall off a cliff.
  - The rolling-average coupled ratio is also computed for reference.
- **Missing days are not imputed as 0.** A missing day leaves both EWMAs **unchanged** (no update),
  and is counted.
  - ACWR = `insufficient_data` if > 1 missing day in the last 7, or > 4 in the last 28.
  - ACWR = `insufficient_data` when the chronic EWMA < `min_chronic` (ratio unstable near 0).
- **Limitations, shown in docs and the dashboard:**
  - it's a ratio of correlated quantities (in the coupled form, mathematically coupled);
  - it's unstable when chronic load is low;
  - the evidence linking it to injury risk is weak and contested (Impellizzeri et al. 2020).
- It's used here only as a **descriptive load-change indicator**. The 0.8–1.3 "sweet spot"
  (Gabbett 2016) is shown as a reference band, not a risk prediction.

## 7. Recovery (0–100)

Components for date D. The sign is set so that **positive = better**:

| component | z | default weight (with HRV) | without HRV |
|---|---|---|---|
| HRV | (lnRMSSD − c₂₈)/s₂₈ | 0.50 | — |
| RHR | −(RHR − c₂₈)/s₂₈ | 0.25 | 0.50 |
| Sleep | (TST − c₂₈)/s₂₈ | 0.25 | 0.50 |

- Each zᵢ is clipped to [−3, 3].
- Combined: Z = Σ wᵢ zᵢ / Σ wᵢ, over the configured components. If a **required** component is
  missing → `insufficient_data`. The score is never silently renormalized onto fewer inputs.
- Mapping: **Recovery = 100 · Φ(Z / σ)**, with Φ the standard normal CDF and
  σ = √(Σ wᵢ²)/Σ wᵢ.
  - With independent components, that makes the score the percentile of today vs your baseline:
    50 = typical, 84 ≈ +1 SD.
  - Components aren't truly independent (RHR and HRV correlate), so read it as "approximately a
    percentile".
  - Bounded, monotone, with no tuning constants beyond the weights.
- **Decomposition**, stored per day: for each component, raw value, baseline center/spread, zᵢ,
  wᵢ, contribution wᵢzᵢ, and points = (score − 50) · wᵢzᵢ / Σ wⱼzⱼ (an exact additive split of
  the distance from 50).
- **Bands** (config): ≥ 67 high, 34–66 moderate, ≤ 33 low.
- **Basis:**
  - lnRMSSD is the best-supported nightly marker for HRV-guided training (Kiviniemi 2007;
    Plews 2013).
  - Elevated RHR relative to baseline is a classic fatigue/illness marker.
  - Sleep restriction impairs performance and recovery.
  - **The weights themselves are judgment calls, not literature values.** That's why they're in
    config and every score can be decomposed.

## 8. Readiness

An ordered rule list in config. The first match wins. A condition is a list of
`{field, op, value}` terms (ANDed). The rules are parsed with a small explicit evaluator; no
`eval`.

```toml
[[readiness.rules]]
when = [{ field = "recovery.status", op = "!=", value = "ok" }]
say  = "Insufficient data for a recommendation"

[[readiness.rules]]
when = [{ field = "z.rhr", op = "<=", value = -2.0 }, { field = "sleep.tst_flag", op = "==", value = true }]
say  = "Possible illness or high fatigue: rest day"

[[readiness.rules]]
when = [{ field = "recovery.score", op = "<", value = 34 }]
say  = "Recovery day recommended"

[[readiness.rules]]
when = [{ field = "acwr.value", op = ">", value = 1.5 }]
say  = "Load spike vs your last 4 weeks: keep intensity down"

[[readiness.rules]]
when = [{ field = "recovery.score", op = ">=", value = 67 }, { field = "acwr.value", op = "<", value = 0.8 }]
say  = "Well recovered and under-loaded: room to push"

[[readiness.rules]]
when = []
say  = "Train as planned"
```

The output records which rule fired and the field values it saw.

## 9. What must wait for real data

| item | why it matters | resolved by |
|---|---|---|
| Sleep stage codes and their meaning | stage mapping table; whether stages are shown at all | first export + your app's sleep screen |
| Time-in-bed vs onset/offset | sleep efficiency vs SME | export |
| Nap and midnight-split representation | episode assembly | export |
| RR intervals present? Format/units? | HRV yes/no; burst vs continuous windows | export / Gadgetbridge inspection |
| Device HRV value and its method | display-only vs usable | export + documentation |
| Background HR cadence; workout HR density | RHR window W, wear gap_max, strain resolution | export (cadence histogram) |
| HR sentinel values | adapter cleaning | export |
| Timestamp format (UTC vs local, s vs ms, offset field) | correct UTC conversion, DST | export |
| Explicit wear/non-wear signal | inferred vs reported wear | export |
| Whether each export is a full snapshot or incremental | ingest strategy sanity | two exports |
| Mi Fitness file layout (`hlth_center_fitness_data.csv` key/value rows?) | adapter | export |

### 9.1 What the Mi Fitness app shows (screenshot, 2026-09-30, a volleyball day)

- Daily HR range 54–148, average 95, **"Resting 68"**, and a second figure "Average heart rate 62".
  These are device-computed daily values. If the export contains them, they go into an optional
  `device_daily_summary` table and are used **only for sanity checks**. Our RHR is the sleeping
  nadir (§5.1), so it should come out *below* 68.
- Zone minutes: Light 71, Intensive 30, Aerobic 22, Anaerobic 0, VO₂max 0. Xiaomi doesn't document
  its zone thresholds, so these can't be compared to ours one-to-one. They're a rough check on how
  much time was spent at higher HR.
- **The peak HR on a playing day was 148.** On these numbers, wrist-measured volleyball doesn't come
  close to the 191 age-predicted max: the auto HRmax stays at 191, and sessions will mostly fall
  at 50–75 % HRR. That's fine for load *trends*, since the error is consistent. A one-off maximal
  effort (e.g. hill repeats with the band started as a workout) would replace the age formula with
  a measured value. Optical HR during arm-heavy play may also under-read; we can't correct that,
  only state it.

## 10. Synthetic generator (Step 2a, outline)

`miaband.sources.synthetic.generate(scenario, seed) -> (CanonicalBatch, GroundTruth)`

- The baseline person has configurable RHR, ln RMSSD mean/SD, HRmax, sleep schedule (bedtime
  mean/SD, duration mean/SD), stage cycles (~90 min), background cadence, and workout cadence.
- **Planted events**, each with known expected outputs in `GroundTruth`:
  - `short_night(date, hours)`
  - `irregular_bedtime(date, shift_min)`
  - `hard_training(date, start, minutes, hrr)`
  - `non_wear(start, end)`
  - `missing_night(date)`
  - `illness(dates, rhr_delta, lnrmssd_delta)`
  - `nap(date, start, minutes)`
  - `dst`: comes for free by placing the scenario across 2026-03-29 or 2026-10-25 (Europe/Madrid)
- RR generation (toggle): an AR(1) RR series with target RMSSD, plus injected ectopic/missed beats
  so the artifact filter is tested against a known clean RMSSD.
- Toggles to mimic each candidate source: `with_stages`, `with_in_bed`, `with_rr`, cadence.
  Features are therefore tested for both "has it" and "doesn't have it" before we know which one
  is real.

## 11. Config (sketch)

```toml
timezone = "Europe/Madrid"

[person]
age = 29
sex = "male"         # selects Banister coefficients
hr_max = ""          # empty = auto (max(220-age, observed reliable max))
resting_hr = 65      # prior until the nightly RHR baseline exists

[sources]
priority = ["gadgetbridge", "mifitness"]

[sufficiency]
day_coverage_min = 0.80
night_hr_coverage_min = 0.80
main_sleep_min_hours = 3.0
episode_merge_gap_min = 60

[baselines]
long_days = 28
long_min_n = 14
short_days = 7
short_min_n = 4
flag_sd = 1.5
spread_floor = { rhr = 1.0, ln_rmssd = 0.05, tst_min = 20 }

[strain]
hrr_floor = 0.30
tau = 120
acwr = { acute = 7, chronic = 28, max_missing_7 = 1, max_missing_28 = 4, min_chronic = 10 }

[recovery]
weights = { hrv = 0.50, rhr = 0.25, sleep = 0.25 }
weights_no_hrv = { rhr = 0.50, sleep = 0.50 }
required = ["rhr", "sleep"]
use_device_hrv = false
bands = { low = 33, high = 67 }
```

## References (short)

- Banister 1991 (TRIMP)
- Edwards 1993 (zone TRIMP)
- Task Force ESC/NASPE 1996 (HRV standards)
- Plews et al. 2012, 2013 (lnRMSSD, rolling averages, SWC)
- Kiviniemi et al. 2007 (HRV-guided training)
- Lipponen & Tarvainen 2019 (RR artifact correction)
- Buchheit 2014 (HR/HRV monitoring)
- Williams et al. 2017 (EWMA ACWR)
- Gabbett 2016
- Impellizzeri et al. 2020 (ACWR critique)
- Phillips et al. 2017 (SRI)
- Windred et al. 2024 (sleep regularity and mortality)
- de Zambotti et al. 2019
- Chinoy et al. 2021 (wearable sleep staging)
- Tanaka et al. 2001 (HRmax)
