"""CONNECTORS: turn core-provided data (entry candles, H1 candles, broker offset) into the `tb` context."""

import numpy as np
import pandas as pd

from utils.mtf import ClosedBarCursor
from .regime import add_regime_columns, regime_trend, regime_codes, MIN_H1_BARS
from .sessions import compute_asian_levels, session_of

TB_TAIL = 60               # closed bars handed to the entry logic
H1_MINUTES = 60


def _bar_minutes(t_utc):
    step = pd.Series(t_utc).diff().median()
    return max(1, int(round(step.total_seconds() / 60))) if pd.notna(step) else 5


# ---------------------------------------------------------------- live

def live_context(md, p):
    """md.entry's last row is the still-forming bar; the signal bar is the last CLOSED one (-2)."""
    df_entry, df_h1 = md.entry, md.extra.get("H1")
    if df_entry is None or len(df_entry) < TB_TAIL + 5:
        return None
    if df_h1 is None or len(df_h1) < MIN_H1_BARS:
        return None

    reg = add_regime_columns(df_h1).iloc[-2]                          # last CLOSED H1 bar
    trend = regime_trend(reg["EMA_F"], reg["EMA_S"], reg["ADX"], p["adx_min"])

    t_utc = pd.DatetimeIndex(df_entry["time"] - pd.Timedelta(seconds=int(md.broker_offset_s)))   # broker -> true UTC
    bar_min = _bar_minutes(t_utc)

    h = df_entry["high"].to_numpy(float)
    l = df_entry["low"].to_numpy(float)
    c = df_entry["close"].to_numpy(float)
    ah, al = compute_asian_levels(t_utc, h, l, bar_min)

    sig = -2
    ts = t_utc[sig]
    return {
        "trend": trend,
        "adx": float(reg["ADX"]) if np.isfinite(reg["ADX"]) else 0.0,
        "ema_f": float(reg["EMA_F"]), "ema_s": float(reg["EMA_S"]),
        "ah": float(ah[sig]), "al": float(al[sig]),
        "hour": float(ts.hour + ts.minute / 60.0),
        "day_key": ts.strftime("%Y-%m-%d"),
        "bar_minutes": bar_min,
        "h": h[:-1][-TB_TAIL:], "l": l[:-1][-TB_TAIL:], "c": c[:-1][-TB_TAIL:],
    }


# ---------------------------------------------------------------- backtest

def prepare(bt, p):
    """Once per run. Heavy, p-independent work (H1 regime, Asian levels) is cached in bt.memo so an
    /optimize sweep computes it once; only the cheap adx_min threshold is applied per run."""
    h1 = bt.extra.get("H1")
    if h1 is None or len(h1) < MIN_H1_BARS:
        raise ValueError("Not enough H1 history to compute the 200 EMA / ADX regime filter.")

    memo = bt.memo
    if "tb_regime" not in memo:
        reg = add_regime_columns(h1)
        if reg["EMA_S"].notna().sum() == 0:
            raise ValueError("Not enough H1 history to compute the 200 EMA / ADX regime filter.")
        memo["tb_regime"] = reg
    reg = memo["tb_regime"]

    if "tb_asian" not in memo:
        t_utc = pd.DatetimeIndex(bt.entry["time"] - pd.Timedelta(seconds=int(bt.broker_offset_s)))
        h = bt.entry["high"].to_numpy(float)
        l = bt.entry["low"].to_numpy(float)
        bar_min = bt.entry_minutes
        ah, al = compute_asian_levels(t_utc, h, l, bar_min)
        memo["tb_asian"] = {
            "ah": ah, "al": al, "bar_min": bar_min,
            "hour": np.asarray(t_utc.hour + t_utc.minute / 60.0, dtype=float),
            "day": np.asarray(t_utc.strftime("%Y-%m-%d")),
            "h": h, "l": l, "c": bt.entry["close"].to_numpy(float),
        }

    return {
        **memo["tb_asian"],
        "codes": regime_codes(reg, p["adx_min"]),
        "cursor": ClosedBarCursor(reg["time"].values, H1_MINUTES),     # per run: it only moves forward
        "t": bt.entry["time"].values,
        "entry_td": np.timedelta64(bt.entry_minutes, "m"),
    }


def bar_context(prep, i, p, state):
    """Skip bars outside a trending regime or outside the London/NY windows before anything else runs."""
    ridx = prep["cursor"].at(prep["t"][i] + prep["entry_td"])          # last H1 bar CLOSED by this bar's end
    if ridx < 0:
        return None
    code = int(prep["codes"][ridx])
    if code == 0:
        return None
    hour = prep["hour"][i]
    if session_of(hour, p) is None:
        return None

    lo = max(0, i - TB_TAIL + 1)
    return {
        "trend": "UP" if code > 0 else "DOWN",
        "ah": prep["ah"][i], "al": prep["al"][i], "hour": hour, "day_key": prep["day"][i],
        "bar_minutes": prep["bar_min"],
        "h": prep["h"][lo:i + 1], "l": prep["l"][lo:i + 1], "c": prep["c"][lo:i + 1],
    }