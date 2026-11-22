"""CONNECTORS: turn core-provided candles into the ctx the rules consume.
Live and backtest both build it through _make_ctx(), so the shape can never drift."""

import numpy as np
import pandas as pd
import pandas_ta as ta

TAIL = 12           # closed bars handed to the rules (covers extreme_bars max of 10)
ADX_LEN = 14
ADX_WARMUP = 30


def _zscore(df, n):
    """Per-bar (z, sma, sd). Causal: bar i uses closes up to and including i."""
    c = df["close"].astype(float)
    sma = c.rolling(n).mean()
    sd = c.rolling(n).std(ddof=0)
    z = (c - sma) / sd.replace(0.0, np.nan)
    return z.to_numpy(float), sma.to_numpy(float), sd.to_numpy(float)


def _adx(df):
    adx = ta.adx(df["high"], df["low"], df["close"], length=ADX_LEN)
    col = f"ADX_{ADX_LEN}"
    if adx is None or col not in adx.columns:
        return np.full(len(df), np.nan)
    return pd.to_numeric(adx[col], errors="coerce").to_numpy(float)


def _make_ctx(a, i, p):
    """ctx for the signal bar at index i of the full arrays `a`; None while history is too short."""
    lo, sb = i - TAIL + 1, int(p["slope_bars"])
    if lo < 0 or i - sb < 0:
        return None
    sl = slice(lo, i + 1)
    return {
        "o": a["o"][sl], "h": a["h"][sl], "l": a["l"][sl], "c": a["c"][sl],
        "z": a["z"][sl], "t": a["t"][sl],
        "sma": float(a["sma"][i]), "sd": float(a["sd"][i]), "adx": float(a["adx"][i]),
        "slope": float(a["sma"][i] - a["sma"][i - sb]),
    }


# ---------------------------------------------------------------- live

def live_context(md, p):
    """md.entry's last row is the still-forming bar; the rules want CLOSED bars only."""
    n = int(p["z_len"])
    df = md.entry
    if df is None or len(df) < n + TAIL + ADX_WARMUP:
        return None
    closed = df.iloc[:-1]
    z, sma, sd = _zscore(closed, n)
    arrays = {
        "o": closed["open"].to_numpy(float), "h": closed["high"].to_numpy(float),
        "l": closed["low"].to_numpy(float), "c": closed["close"].to_numpy(float),
        "t": closed["time"].values, "z": z, "sma": sma, "sd": sd, "adx": _adx(closed),
    }
    return _make_ctx(arrays, len(closed) - 1, p)


# ---------------------------------------------------------------- backtest

def prepare(bt, p):
    """Once per run. ADX is p-independent, so it is cached in bt.memo across an /optimize sweep."""
    e = bt.entry
    if "zs_adx" not in bt.memo:
        bt.memo["zs_adx"] = _adx(e)
    z, sma, sd = _zscore(e, int(p["z_len"]))
    return {
        "o": e["open"].to_numpy(float), "h": e["high"].to_numpy(float),
        "l": e["low"].to_numpy(float), "c": e["close"].to_numpy(float),
        "t": e["time"].values, "z": z, "sma": sma, "sd": sd, "adx": bt.memo["zs_adx"],
    }


def bar_context(prep, i, p, state):
    """Skip bars cheaply unless the PRIOR bar reached the entry extreme."""
    if i < TAIL or not (abs(prep["z"][i - 1]) >= float(p["entry_z"])):
        return None
    return _make_ctx(prep, i, p)