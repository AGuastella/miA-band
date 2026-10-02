"""Recovery score 0-100 and readiness rules (docs/SPEC.md §7-8).

Recovery (this source has no HRV, so the title says so):
  z_i      = component z vs the personal 28-day baseline, signed so that positive = better,
             clipped to [-3, 3]:  RHR -> -z(RHR),  sleep -> +z(TST)
  Z        = sum(w_i z_i) / sum(w_i)                     (weights from config)
  score    = 100 * Phi(Z / sigma),  sigma = sqrt(sum w_i^2) / sum w_i
             = the percentile of today vs baseline if components were independent normals
  points_i = (score - 50) * w_i z_i / sum_j w_j z_j      (exact additive split of score - 50)
A missing required component -> no score ('insufficient_data' / 'warming_up'); the score is
never silently computed from fewer inputs. The weights are judgment, not literature values.

Readiness: an ordered rule list from config; the first rule whose conditions all hold wins.
Conditions are {field, op, value} terms evaluated by a small explicit evaluator (no eval). A term
on a missing value is false, so rules about ACWR simply don't fire while ACWR is unavailable.
"""
from __future__ import annotations

import operator

import numpy as np
import pandas as pd
from scipy.stats import norm

from ..config import Config

OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
       "==": operator.eq, "!=": operator.ne}
# readiness field -> wide-frame column
FIELDS = {
    "recovery.status": "recovery_status", "recovery.score": "recovery", "recovery.band": "recovery_band",
    "rhr.z": "rhr_z", "rhr.flag": "rhr_flag", "sleep.z": "tst_z", "sleep.flag": "tst_flag",
    "sleep.tst_min": "tst_min", "acwr.value": "acwr", "acwr.status": "acwr_status",
    "load.yesterday": "load_yesterday",
}
# component -> (z column, baseline-status column, sign so that positive = better)
COMPONENTS = {"rhr": ("rhr_z", "rhr_base_status", -1.0), "sleep": ("tst_z", "tst_base_status", +1.0)}


def recovery(wide: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    r = cfg.recovery
    weights = {k: float(v) for k, v in r.weights.items() if k in COMPONENTS}
    if set(r.required) - set(weights):
        raise ValueError(f"recovery.required {r.required} must be among weighted components {list(weights)}")
    wsum = sum(weights.values())
    sigma = np.sqrt(sum(w * w for w in weights.values())) / wsum
    out = pd.DataFrame({"date": wide["date"]})
    zs = {}
    avail = pd.Series(True, index=wide.index)
    for comp, w in weights.items():
        zcol, stcol, sign = COMPONENTS[comp]
        z = (sign * wide[zcol]).clip(-3, 3) if zcol in wide else pd.Series(np.nan, index=wide.index)
        ok = (wide.get(stcol) == "ok") & z.notna() if stcol in wide else pd.Series(False, index=wide.index)
        zs[comp] = z.where(ok)
        out[f"recovery_z_{comp}"] = zs[comp]
        if comp in r.required:
            avail &= ok
    Z = sum(weights[c] * zs[c].fillna(0) for c in weights) / wsum
    score = pd.Series(100 * norm.cdf(Z / sigma), index=wide.index).where(avail)
    out["recovery"] = score
    contrib_total = sum(weights[c] * zs[c].fillna(0) for c in weights)
    for c in weights:
        share = (weights[c] * zs[c].fillna(0)) / contrib_total.replace(0, np.nan)
        out[f"recovery_pts_{c}"] = ((score - 50) * share).where(avail).fillna(0.0).where(avail)
    lo, hi = r.bands["low"], r.bands["high"]
    out["recovery_band"] = np.select([score <= lo, score >= hi, score.notna()], ["low", "high", "moderate"], None)
    warming = pd.Series(False, index=wide.index)
    for c in r.required:
        warming |= wide.get(COMPONENTS[c][1]) == "warming_up"
    out["recovery_status"] = np.where(avail, "ok", np.where(warming, "warming_up", "insufficient_data"))
    out["recovery_reason"] = np.where(
        avail, None, np.where(warming, "baseline needs 14 valid nights on this device",
                              "resting HR or sleep missing for this night"))
    return out


def _value(row: pd.Series, field: str):
    v = row.get(FIELDS[field])
    if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA:
        return None
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    return v


def readiness(wide: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    rules = cfg.readiness.rules
    for i, rule in enumerate(rules):
        for t in rule.get("when", []):
            if t["field"] not in FIELDS or t["op"] not in OPS:
                raise ValueError(f"readiness rule {i}: bad term {t}; fields {sorted(FIELDS)}, ops {list(OPS)}")
    out = []
    for _, row in wide.iterrows():
        hit, seen = None, {}
        for i, rule in enumerate(rules):
            ok = True
            for t in rule.get("when", []):
                v = _value(row, t["field"])
                seen[t["field"]] = v
                if v is None or not OPS[t["op"]](v, t["value"]):
                    ok = False
                    break
            if ok:
                hit = i
                break
        out.append(dict(date=row["date"], readiness_rule=hit,
                        readiness=rules[hit]["say"] if hit is not None else None,
                        readiness_inputs=", ".join(f"{k}={v}" for k, v in seen.items())))
    return pd.DataFrame(out)


def add_recovery_and_readiness(wide: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    w = wide.copy()
    w["load_yesterday"] = w["load"].shift(1) if "load" in w else np.nan
    w = w.merge(recovery(w, cfg), on="date", how="left")
    return w.merge(readiness(w, cfg), on="date", how="left")
