"""Asian range + session windows. TRUE UTC throughout. Pure."""

import numpy as np
import pandas as pd

ASIAN_START_H = 0.0
ASIAN_END_H = 7.0


def compute_asian_levels(times_utc, highs, lows, bar_minutes, min_fill=0.6):
    """
    Per-bar arrays (asian_high, asian_low) of the Asian session (00:00-07:00 UTC) of that
    bar's UTC date. NaN while the session is still running, or if too many bars are missing.
    """
    idx = pd.DatetimeIndex(times_utc)
    day = idx.normalize()
    hour = np.asarray(idx.hour + idx.minute / 60.0, dtype=float)
    asian = (hour >= ASIAN_START_H) & (hour < ASIAN_END_H)
    expected = (ASIAN_END_H - ASIAN_START_H) * 60.0 / max(bar_minutes, 1)

    frame = pd.DataFrame({"day": day[asian],
                          "h": np.asarray(highs, dtype=float)[asian],
                          "l": np.asarray(lows, dtype=float)[asian]})
    g = frame.groupby("day").agg(ah=("h", "max"), al=("l", "min"), n=("h", "size"))
    g = g[g["n"] >= expected * min_fill]

    ah = pd.Series(day).map(g["ah"]).to_numpy(dtype=float, copy=True)
    al = pd.Series(day).map(g["al"]).to_numpy(dtype=float, copy=True)
    not_done = hour < ASIAN_END_H
    ah[not_done] = np.nan
    al[not_done] = np.nan
    return ah, al


def session_of(hour, p):
    """'LONDON' | 'NY' | None for a UTC fractional hour (bar OPEN time)."""
    w = float(p["window_hours"])
    for name, start in (("LONDON", float(p["london_start"])), ("NY", float(p["ny_start"]))):
        if start <= hour < start + w:
            return name
    return None