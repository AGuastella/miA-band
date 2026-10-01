# miA-band: Step 1 spec (source-agnostic)

Status: **draft, awaiting approval** (updated with the Mi Fitness export findings, §9.2). Everything below is defined against a canonical internal
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

### 6.2 Training load

**Primary (decided after the Mi Fitness findings, §9.2): Edwards TRIMP per workout**
= Σ zone-minutes × weight (zones 1–5 → weights 1–5), from the band's own zone durations.
Daily load = Σ over that day's workouts. A day with no workout has load 0. That is a real zero,
not an imputation, *provided the band was worn that day* (wear coverage per §3). Otherwise the day
is `insufficient_data`.

**Secondary: Banister TRIMP** from background HR, on days with ≤ 2-min cadence:

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

**As implemented (Step 3, approved 2026-10-01).** Only 237 of 818 workouts carry band zone
durations, so the zone minutes are computed **by us** from the HR samples inside each workout
window (Edwards zones on %HRmax, weights 1–5). This keeps one definition for all years and
devices.
- Requirement for "our" zones: HR at ≤ 2-min cadence covering ≥ 80 % of the workout. Each sample
  holds until the next one, capped at 2 × cadence.
- Fallback 1: the band's zone durations (`device_zones`).
- Fallback 2: none. The workout load is unknown and the day is `insufficient_data`. Nothing is
  approximated from average HR.
- `miaband sanity` compares ours against the band's zones wherever both exist, and checks the
  implied HRmax of the band's zones (avg HR / time-weighted zone midpoint).
- A workout-free day is a real 0 only if the band was worn ≥ 70 % of 08:00–22:00 local;
  otherwise it is unknown.
- Banister TRIMP (HRr ≥ 0.30, ≤ 2-min cadence) is computed per day as a secondary value. It is
  only used to flag "high HR without a recorded workout?".
- HRrest for Banister = the trailing 28-day nightly-RHR baseline.
- HRmax per date = max(220 − age at that date, median of the 3 highest workout maxima in the last
  24 months, ignoring > 220 − age + 15).
- τ = "auto": the median workout day maps to strain 12/21.

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

### 9.2 Findings from the Mi Fitness export (2026-10-01, ~7.3 M rows)

Source: `inspect_mifitness.py` report on the full export. These resolve most of the §9 table.

**Files that matter.**
- `hlth_center_fitness_data.csv` (806 MB): columns `Uid, Sid, Key, Time, Value(JSON), UpdateTime`.
  This is the primary source.
- `hlth_center_sport_record.csv`: one row per workout (818 since 2017), summaries only.
- `user_fitness_data_records.csv` is the legacy (Mi Fit / Zepp era) store. It largely duplicates
  the main file, with one exception: `watch_hrm_record.raw_hrm`, a base64 per-minute HR blob per
  day. That blob may fill the HR gaps of 2018–2019 (only 16 k HR rows in 2019 in the main file).
  It's a **later, optional** enhancement.
- Everything else is empty or irrelevant (settings, profile, GPS).

**Device eras** (`Sid`; there are no duplicate timestamps within a key):

| era | Sid | dates | background HR cadence | sleep record |
|---|---|---|---|---|
| A | `b4e8c92d` | 2017-04 → 2025-06-12 | 60 s (sparse in 2018–19) | `watch_night_sleep` + `watch_daytime_sleep` |
| B | `cef93f20` | 2025-06-12 → 08-20 | **600 s** | `watch_night_sleep` |
| C | `431c4ef0` | 2025-08-21 → 2026-09-08 | 60 s, ~1440/day | `sleep` (night + naps) |
| D | `ac7b7f65` (Band 10) | 2026-09-08 → now | **600 s** | `sleep` |

- Era A is the Mi Fit/Zepp import. Several bands (Mi Band 1→…) are merged under one source id, so
  band changes *inside* era A can't be seen from the id.
- Consequence for baselines: the 28-day baseline **restarts at every era boundary** (B, C, D).
  Inside era A it doesn't restart, and that is a documented limitation.

**Keys → canonical tables.**

| key | → | notes |
|---|---|---|
| `heart_rate` `{time,bpm,type=0}` | `hr_samples` (context `background`) | bpm 37–217; range filter in features |
| `single_heart_rate` | `hr_samples` (context `spot`) | manual measurements; not used for RHR/strain |
| `watch_night_sleep`, `sleep` | `sleep_sessions` + `sleep_segments` | same JSON shape: `bedtime, wake_up_time, duration, sleep_{deep,light,rem,awake}_duration, items[]{start_time,end_time,state}` |
| `watch_daytime_sleep` | `sleep_sessions` (`is_nap`) | `bedtime=0`; the session envelope comes from `items[]` |
| `sport_record` rows | `workouts` | plus HR-zone seconds, avg/max/min HR, device train_load/effect |
| `resting_heart_rate` `{date_time,bpm}`, sleep `avg_hr/min_hr/max_hr`, `training_load`, `vitality`, `pai`, `vo2_max`, `stress` | `device_daily_summary` | **reference only**, never an input to our scores |
| `steps` (per minute) | `step_samples` | used for wear inference |
| `body_momentum`, `light_sensitivity_value` | — | every value is 0: dropped |
| `intensity` | — | only a timestamp, no value: dropped |

**Sleep stage codes.**
- **2 = deep** and **3 = light** are verified: per night, Σ item minutes by state equals
  `sleep_deep_duration` / `sleep_light_duration` exactly.
- **4 = REM** and **5 = awake** are inferred: they appear only on nights with non-zero
  `sleep_rem_duration` / `sleep_awake_duration`, and their frequencies match.
- **0** appears only in era-A nights from 2018: an unclassified first hour, mapped to `asleep`.
- The adapter **verifies the mapping on every night**. It recomputes stage minutes from the items,
  compares them with the duration fields, reports the agreement rate, and fails if it falls below
  95 %.
- REM is 0 on whole stretches of era A (older bands had no REM detection). Stage breakdowns are
  therefore shown only for nights where the device produced REM.

**Time in bed.**
- The new `sleep` key has `bed_timestamp` / `out_bed_timestamp`. But in-bed starts a fixed ~4 min
  before sleep onset and ends exactly at wake, and the device's `sleep_efficiency` sits at 95–98 %
  almost always.
- That is an algorithmic envelope, not a measured bed entry. Per §4.2 → report **sleep maintenance
  efficiency** and WASO, never "sleep efficiency".
- WASO will often be 0: the band rarely scores brief awakenings. This is stated in the docs.

**Time zones.**
- Every sleep and workout record carries `timezone` in **15-min units**: 4 = UTC+1, 8 = UTC+2.
- Travel shows up as 0, 12, 22 (UTC+5:30), 32 and 36.
- In era A, summer nights mostly carry 4, which suggests the old app stored the *standard* offset
  without DST.
- Decision (**no user input needed**): local time comes from the **offset stored on the records
  themselves**, not from a configured home zone.
  - Each sleep/workout record gives the offset in force that day. HR and step samples take the
    offset of the nearest sleep/workout record (sleep records exist almost daily).
  - Era A stored the standard offset without DST. For offsets 0 and +1 (every place in Europe with
    those offsets follows the same EU rule: last Sunday of March → last Sunday of October, 01:00
    UTC), the adapter applies that rule to recover the real wall-clock offset.
  - Other offsets (+3, +5:30, +8, +9) are used as is; those places don't observe DST.
  - Live data with no record yet for the day falls back to `config.timezone` (Europe/Madrid, which
    also covers Barcelona).
- Why it matters: it decides which date a night or a training day belongs to, and the clock times
  behind bedtime regularity. Without it, a trip to UTC+8 would look like a 7-hour bedtime shift,
  and old summer nights would look an hour early.
- A record whose offset differs from the previous day's is flagged `tz_change`. The first two
  nights after it are excluded from the regularity metrics (jet lag is real, but it isn't
  irregular habit).

**Bad timestamps.**
- A handful of rows are dated 2000-12-31 (device clock not set). The adapter drops anything before
  2015-01-01, and anything after the export date.

**HRV.**
- **No RR intervals and no device HRV anywhere in the export.**
- The two name-based "hits" are false positives: `abnormalHeartbeatEnable`, and a random file-name
  fragment.
- → Recovery runs as **"Recovery (HR + sleep, no HRV)"** per §5.2. This holds unless Gadgetbridge
  later exposes RR data for the Band 10.

**Workout HR.**
- The export has **no per-second workout HR**, only session summaries, so strain has to come from
  what is actually there:
  1. **Background 1-min HR** (eras A and C) → daily Banister TRIMP as specified in §6.2. At
     10-min cadence (eras B and D, i.e. **the Band 10 right now**) it falls below the
     resolution gate.
  2. **Workout zone durations** → `sport_record` gives seconds in 5 HR zones
     (`hrm_warm_up / fat_burning / aerobic / anaerobic / extreme`, `reserve_hr_zone = 0`).
     - If those zones are Xiaomi's %HRmax bands (50–60 / 60–70 / 70–80 / 80–90 / 90–100 % of max
       HR), that is exactly **Edwards' TRIMP** = Σ zone-minutes × weights 1..5.
     - It exists for every recorded workout since 2017 and doesn't depend on background cadence.
     - It is computed from the device's zone classification, which we can't audit. The thresholds
       couldn't be found in the app, so the assumption is **checked from the data**:
       - For workouts whose zone time covers ≥ 90 % of the duration, the time-weighted zone
         midpoint (55/65/75/85/95 %) × HRmax should reproduce `avg_hrm`.
       - Fitting HRmax_implied = avg_hrm / weighted-midpoint across sessions estimates the HRmax
         the band uses. A tight spread (e.g. CV < 5 %) supports %HRmax zones.
       - A spread that depends on resting HR would point to HR-reserve zones; then the Edwards
         weights get relabelled "device-zone TRIMP".
     - **Decision (approved): Edwards TRIMP from workout zones is the primary training load**
       (§6.2 updated). Banister from 1-min background HR is secondary: validation, and it catches
       sessions not started on the band.

**HRmax.**
- Workout `max_hrm` reaches 188–194 (football, beach volleyball, free training). That's consistent
  with 220 − 29 = 191.
- Auto HRmax = median of the 3 highest session `max_hrm` values in the last 24 months, capped at
  220 − age + 15 to reject artifacts.
- The single-sample `heart_rate` maximum (217) is not used.

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
