"""CONNECTORS: turn core-provided candles into the ctx the rules consume.
Live and backtest produce the SAME ctx shape, so rules.py never knows which one it is in."""

import numpy as np
import pandas as pd

TAIL = 120          # closed bars handed to the rules (> box_max_bars max of 100 + warm-up margin)


def _squeeze(df, p):
    """Bollinger Bands fully inside Keltner Channels, per bar (causal: bar i uses data up to i)."""
    c = df["close"].astype(float)
    mid = c.rolling(int(p["bb_len"])).mean()
    sd = c.rolling(int(p["bb_len"])).std(ddof=0)
    bb_u, bb_l = mid + float(p["bb_mult"]) * sd, mid - float(p["bb_mult"]) * sd
    kc_mid = c.ewm(span=int(p["kc_len"]), adjust=False).mean()
    kc_u = kc_mid + float(p["kc_mult"]) * df["ATR"]
    kc_l = kc_mid - float(p["kc_mult"]) * df["ATR"]
    return ((bb_u < kc_u) & (bb_l > kc_l)).to_numpy(dtype=bool)


def _hours(times, offset_s):
    """Broker-server timestamps -> true-UTC fractional hour of each bar's open."""
    t_utc = pd.DatetimeIndex(times - pd.Timedelta(seconds=int(offset_s)))
    return np.asarray(t_utc.hour + t_utc.minute / 60.0, dtype=float)


# ---------------------------------------------------------------- live

def live_context(md, p):
    """md.entry's last row is the still-forming bar; the rules want CLOSED bars only."""
    df = md.entry
    if df is None or len(df) < TAIL + 2:
        return None
    closed = df.iloc[:-1]
    sq = _squeeze(closed, p)
    hours = _hours(closed["time"], md.broker_offset_s)
    ema20, ema50 = closed["EMA_20"].iloc[-1], closed["EMA_50"].iloc[-1]
    tail = closed.tail(TAIL)
    return {
        "o": tail["open"].to_numpy(float), "h": tail["high"].to_numpy(float),
        "l": tail["low"].to_numpy(float), "c": tail["close"].to_numpy(float),
        "sq": sq[-TAIL:], "t": tail["time"].values, "hour": float(hours[-1]),
        "ema_bull": bool(pd.notna(ema20) and pd.notna(ema50) and ema20 > ema50),
    }


# ---------------------------------------------------------------- backtest

def prepare(bt, p):
    """Once per run. The p-independent hour array is cached in bt.memo across an /optimize sweep."""
    e = bt.entry
    memo = bt.memo
    if "vc_hour" not in memo:
        memo["vc_hour"] = _hours(e["time"], bt.broker_offset_s)
    return {
        "o": e["open"].to_numpy(float), "h": e["high"].to_numpy(float),
        "l": e["low"].to_numpy(float), "c": e["close"].to_numpy(float),
        "t": e["time"].values, "hour": memo["vc_hour"],
        "ema20": e["EMA_20"].to_numpy(float), "ema50": e["EMA_50"].to_numpy(float),
        "sq": _squeeze(e, p),
    }


def bar_context(prep, i, p, state):
    """Skip bars cheaply unless the PRIOR bar was still in a squeeze."""
    if i < TAIL or not prep["sq"][i - 1]:
        return None
    lo = i - TAIL + 1
    sl = slice(lo, i + 1)
    return {
        "o": prep["o"][sl], "h": prep["h"][sl], "l": prep["l"][sl], "c": prep["c"][sl],
        "sq": prep["sq"][sl], "t": prep["t"][sl], "hour": float(prep["hour"][i]),
        "ema_bull": bool(prep["ema20"][i] > prep["ema50"][i]),
    }