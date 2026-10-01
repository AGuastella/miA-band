"""Sleep episodes and wake-date assignment (docs/SPEC.md §4.1).

1. Per wake date, keep only the highest-priority source (sources are never mixed within a day).
2. Overlapping sessions from the same source are duplicates (e.g. the same night stored under
   two keys around a device switch): keep the longer one.
3. Sessions < `episode_merge_gap_min` apart are merged into one episode (a brief wake doesn't
   split the night).
4. Wake date = local date of the episode end. Main sleep = the longest non-nap episode of at
   least `main_sleep_min_hours` for that date; every other episode is a nap.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Config
from .timeutil import local_date, local_offsets, zone_offsets


def _offsets_for(sessions: pd.DataFrame, col: str, timeline, cfg: Config) -> np.ndarray:
    """Offset at the `col` endpoint of each session.

    The session's own recorded offset wins, except when it is the home zone's offset at the
    session's start or end: then the home zone's DST-aware offset at this endpoint is used, so a
    night spanning a DST switch gets the right wall-clock wake time. No record -> timeline ->
    fallback zone.
    """
    from_tl = local_offsets(sessions[col].to_numpy(), timeline, cfg.tz)
    own = sessions["tz_offset_min"].to_numpy(dtype=float)
    zone_here = zone_offsets(sessions[col].to_numpy(), cfg.tz)
    in_home = ((own == zone_offsets(sessions["start_ts"].to_numpy(), cfg.tz))
               | (own == zone_offsets(sessions["end_ts"].to_numpy(), cfg.tz)))
    out = np.where(np.isnan(own), from_tl, np.where(in_home, zone_here, own))
    return out.astype("int64")


def resolve_sources(sessions: pd.DataFrame, priority: tuple[str, ...], timeline, cfg: Config) -> pd.DataFrame:
    if sessions.empty:
        return sessions
    rank = {s: i for i, s in enumerate(priority)}
    d = sessions.copy()
    d["_wake"] = local_date(d["end_ts"], _offsets_for(d, "end_ts", timeline, cfg))
    d["_rank"] = d["source"].map(lambda s: rank.get(s, len(rank)))
    best = d.groupby("_wake")["_rank"].transform("min")
    return d[d["_rank"] == best].drop(columns=["_wake", "_rank"])


def drop_overlaps(sessions: pd.DataFrame) -> pd.DataFrame:
    """Within a source, overlapping sessions are the same sleep stored twice: keep the longer."""
    keep = []
    for _, g in sessions.sort_values(["source", "start_ts"], kind="stable").groupby("source", sort=False):
        cur = None
        for row in g.itertuples():
            if cur is not None and row.start_ts < cur.end_ts:
                if (row.end_ts - row.start_ts) > (cur.end_ts - cur.start_ts):
                    cur = row
                continue
            if cur is not None:
                keep.append(cur.Index)
            cur = row
        if cur is not None:
            keep.append(cur.Index)
    return sessions.loc[sorted(keep)]


def build_episodes(sessions: pd.DataFrame, timeline: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """One row per episode with its member sessions and role ('main' | 'nap')."""
    cols = ["episode", "source", "device_id", "start_ts", "end_ts", "offset_start", "offset_end",
            "wake_date", "is_nap", "role", "members"]
    if sessions.empty:
        return pd.DataFrame(columns=cols)
    s = resolve_sources(sessions, cfg.sources.priority, timeline, cfg)
    s = drop_overlaps(s).sort_values("start_ts", kind="stable").reset_index(drop=True)
    s["off_start_"] = _offsets_for(s, "start_ts", timeline, cfg)
    s["off_end_"] = _offsets_for(s, "end_ts", timeline, cfg)

    gap_s = cfg.sufficiency.episode_merge_gap_min * 60
    episodes = []
    cur = None
    for row in s.itertuples(index=False):
        nap = row.is_nap == 1
        if (cur is not None and row.source == cur["source"] and nap == cur["is_nap"]
                and row.start_ts - cur["end_ts"] < gap_s):
            cur["members"].append((row.source, row.device_id, int(row.start_ts)))
            if row.end_ts > cur["end_ts"]:
                cur["end_ts"], cur["offset_end"] = int(row.end_ts), int(row.off_end_)
            if (row.end_ts - row.start_ts) > cur["_longest"]:
                cur["device_id"], cur["_longest"] = row.device_id, row.end_ts - row.start_ts
            continue
        if cur is not None:
            episodes.append(cur)
        cur = dict(source=row.source, device_id=row.device_id, start_ts=int(row.start_ts),
                   end_ts=int(row.end_ts), offset_start=int(row.off_start_),
                   offset_end=int(row.off_end_), is_nap=nap, _longest=row.end_ts - row.start_ts,
                   members=[(row.source, row.device_id, int(row.start_ts))])
    episodes.append(cur)

    ep = pd.DataFrame(episodes).drop(columns="_longest")
    ep["wake_date"] = local_date(ep["end_ts"], ep["offset_end"])
    ep["duration_h"] = (ep["end_ts"] - ep["start_ts"]) / 3600
    eligible = ~ep["is_nap"] & (ep["duration_h"] >= cfg.sufficiency.main_sleep_min_hours)
    ep["role"] = "nap"
    longest = ep[eligible].sort_values("duration_h", ascending=False, kind="stable").drop_duplicates("wake_date")
    ep.loc[longest.index, "role"] = "main"
    ep["episode"] = np.arange(len(ep))
    return ep[cols + ["duration_h"]]


def episode_segments(episodes: pd.DataFrame, segments: pd.DataFrame) -> pd.DataFrame:
    """Segments of every episode's member sessions, tagged with the episode id."""
    if episodes.empty or segments.empty:
        return pd.DataFrame(columns=list(segments.columns) + ["episode"])
    link = pd.DataFrame([(e, *m) for e, ms in zip(episodes["episode"], episodes["members"]) for m in ms],
                        columns=["episode", "source", "device_id", "session_start_ts"])
    seg = segments.astype({"session_start_ts": "int64"})
    return seg.merge(link, on=["source", "device_id", "session_start_ts"], how="inner")
