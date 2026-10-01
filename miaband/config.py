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
    tz_change_exclude_nights: int = 2


@dataclass(frozen=True)
class Physiology:
    rhr_skip_onset_min: float = 30.0
    rhr_window_min: float = 10.0
    rhr_window_min_fill: float = 0.70


@dataclass(frozen=True)
class Baselines:
    long_days: int = 28
    long_min_n: int = 14
    short_days: int = 7
    short_min_n: int = 4
    flag_sd: float = 1.5
    spread_floor: dict = field(default_factory=lambda: {"rhr": 1.0, "tst_min": 20.0, "sleep_hr_mean": 1.0})


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
