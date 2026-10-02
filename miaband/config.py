"""Configuration: TOML file -> typed, validated dataclasses. Every field has a default so a
missing file or section still yields a complete, documented configuration."""
from __future__ import annotations

import datetime as dt
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Person:
    age: int = 29
    age_as_of: dt.date = dt.date(2026, 10, 1)
    sex: str = "male"
    hr_max: int | None = None
    resting_hr: float = 65.0

    def age_on(self, day: dt.date) -> float:
        """Age at a past date (strain over 9 years must use the age at the time)."""
        return self.age - (self.age_as_of - day).days / 365.25


@dataclass(frozen=True)
class Sources:
    priority: tuple[str, ...] = ("gadgetbridge", "mifitness", "synthetic")


@dataclass(frozen=True)
class Sufficiency:
    night_hr_coverage_min: float = 0.80
    main_sleep_min_hours: float = 3.0
    episode_merge_gap_min: float = 60.0
    wear_gap_min_floor: float = 10.0


@dataclass(frozen=True)
class Sleep:
    regularity_window_days: int = 7
    regularity_min_nights: int = 5
    sri_min_pair_coverage: float = 0.5
    tz_change_exclude_nights: int = 2


@dataclass(frozen=True)
class Physiology:
    rhr_skip_onset_min: float = 30.0
    rhr_window_min: float = 10.0
    rhr_window_min_fill: float = 0.70


@dataclass(frozen=True)
class Strain:
    workout_max_cadence_s: float = 120.0   # our own Edwards needs HR at least every 2 min ...
    workout_min_coverage: float = 0.80     # ... covering this share of the workout
    day_wear_window: tuple = (8, 22)       # local hours checked to call a workout-free day a real 0
    day_wear_min: float = 0.70
    hrmax_lookback_days: int = 730
    hrmax_artifact_margin: float = 15.0    # observed maxima above 220-age+15 are rejected
    tau: float | str = "auto"              # readability scale; "auto": median workout day -> 12/21
    hrr_floor: float = 0.30                # Banister (secondary) counts samples at >= 30 % HRR
    banister_max_cadence_s: float = 120.0
    unrecorded_hint_banister: float = 40.0
    acwr_acute_days: int = 7
    acwr_chronic_days: int = 28
    acwr_max_missing_7: int = 1
    acwr_max_missing_28: int = 4
    acwr_min_chronic: float = 10.0


@dataclass(frozen=True)
class Baselines:
    long_days: int = 28
    long_min_n: int = 14
    short_days: int = 7
    short_min_n: int = 4
    flag_sd: float = 1.5
    spread_floor: dict = field(default_factory=lambda: {"rhr": 1.0, "tst_min": 20.0, "sleep_hr_mean": 1.0})


DEFAULT_RULES = (
    {"when": [{"field": "recovery.status", "op": "!=", "value": "ok"}],
     "say": "Insufficient data for a recommendation"},
    {"when": [{"field": "rhr.z", "op": ">=", "value": 2.0}, {"field": "sleep.flag", "op": "==", "value": True}],
     "say": "Resting HR well above baseline after a short night: possible illness or high fatigue, rest"},
    {"when": [{"field": "recovery.score", "op": "<", "value": 34}],
     "say": "Recovery day recommended"},
    {"when": [{"field": "acwr.value", "op": ">", "value": 1.5}],
     "say": "Load spike vs your last 4 weeks: keep intensity down"},
    {"when": [{"field": "recovery.score", "op": ">=", "value": 67}, {"field": "acwr.value", "op": "<", "value": 0.8}],
     "say": "Well recovered and under-loaded: room to push"},
    {"when": [], "say": "Train as planned"},
)


@dataclass(frozen=True)
class Recovery:
    weights: dict = field(default_factory=lambda: {"rhr": 0.5, "sleep": 0.5})
    required: tuple = ("rhr", "sleep")
    bands: dict = field(default_factory=lambda: {"low": 33, "high": 67})


@dataclass(frozen=True)
class Readiness:
    rules: tuple = DEFAULT_RULES


@dataclass(frozen=True)
class Config:
    timezone: str = "Europe/Madrid"
    store: Path = Path("data/miaband.sqlite")
    person: Person = field(default_factory=Person)
    sources: Sources = field(default_factory=Sources)
    sufficiency: Sufficiency = field(default_factory=Sufficiency)
    sleep: Sleep = field(default_factory=Sleep)
    physiology: Physiology = field(default_factory=Physiology)
    baselines: Baselines = field(default_factory=Baselines)
    strain: Strain = field(default_factory=Strain)
    recovery: Recovery = field(default_factory=Recovery)
    readiness: Readiness = field(default_factory=Readiness)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _build(cls, data: dict, path: str):
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"unknown config keys in [{path or 'root'}]: {sorted(unknown)}")
    kwargs = {}
    for name, f in known.items():
        if name not in data:
            continue
        value = data[name]
        default = f.default_factory() if callable(f.default_factory) else f.default
        if is_dataclass(default):
            value = _build(type(default), value, f"{path}.{name}".strip("."))
        elif isinstance(default, Path):
            value = Path(value)
        elif isinstance(default, tuple):
            value = tuple(value)
        elif isinstance(default, dict):
            value = {**default, **value}
        elif isinstance(default, dt.date) and isinstance(value, str):
            value = dt.date.fromisoformat(value)
        kwargs[name] = value
    return cls(**kwargs)


def load_config(path: Path | str | None = None) -> Config:
    if path is None or not Path(path).exists():
        cfg = Config()
    else:
        with open(path, "rb") as fh:
            cfg = _build(Config, tomllib.load(fh), "")
    ZoneInfo(cfg.timezone)  # fail early on a bad zone name
    if cfg.person.sex not in ("male", "female"):
        raise ValueError("person.sex must be 'male' or 'female'")
    return cfg
