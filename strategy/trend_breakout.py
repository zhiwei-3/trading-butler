# strategy/trend_breakout.py
"""
Systematic Trend Following / Volatility Breakout.

Shared by the live evaluator and the backtester so the two cannot drift apart.

Regime  : H1 EMA50 vs EMA200 + ADX(14) > tb_adx_min  (last CLOSED H1 bar)
Entry   : inside the London / NY open window, in the regime direction
            SWEEP_BOS : Asian high/low swept, then a BOS in the trend direction
            BREAKOUT  : candle CLOSE beyond the Asian range
Targets : structural SL (+ fractional ATR buffer, ATR floor/cap) and fixed-RR TPs

All time logic is TRUE UTC (broker-server time minus the detected offset).
"""

import numpy as np
import pandas as pd
import pandas_ta as ta
import MetaTrader5 as mt5

from config import ALERT_STATE
from mt5_engine import fetch_candles, get_broker_utc_offset_seconds

ASIAN_START_H = 0.0
ASIAN_END_H = 7.0
TB_TAIL = 60                       # closed bars handed to the entry logic
REGIME_EMA_FAST, REGIME_EMA_SLOW, REGIME_ADX_LEN = 50, 200, 14


# ---------------------------------------------------------------- regime

def add_regime_columns(df):
    """Adds EMA_F (50), EMA_S (200) and ADX (14) to an H1 frame."""
    df = df.copy()
    df["EMA_F"] = pd.to_numeric(ta.ema(df["close"], length=REGIME_EMA_FAST), errors="coerce")
    df["EMA_S"] = pd.to_numeric(ta.ema(df["close"], length=REGIME_EMA_SLOW), errors="coerce")
    adx = ta.adx(df["high"], df["low"], df["close"], length=REGIME_ADX_LEN)
    col = f"ADX_{REGIME_ADX_LEN}"
    df["ADX"] = pd.to_numeric(adx[col], errors="coerce") if adx is not None and col in adx.columns else np.nan
    return df


def regime_trend(ema_f, ema_s, adx):
    """'UP' | 'DOWN' | None (None = chop / not enough data)."""
    if not (np.isfinite(ema_f) and np.isfinite(ema_s) and np.isfinite(adx)):
        return None
    if adx <= ALERT_STATE["tb_adx_min"]:
        return None
    if ema_f > ema_s:
        return "UP"
    if ema_f < ema_s:
        return "DOWN"
    return None


def regime_codes(df):
    """Vectorised regime for the backtester: +1 up, -1 down, 0 none."""
    ef, es, adx = (df[k].to_numpy(float) for k in ("EMA_F", "EMA_S", "ADX"))
    ok = np.isfinite(ef) & np.isfinite(es) & np.isfinite(adx) & (adx > ALERT_STATE["tb_adx_min"])
    return np.where(ok & (ef > es), 1, np.where(ok & (ef < es), -1, 0))


# ---------------------------------------------------------------- Asian range / sessions

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


def session_of(hour):
    """'LONDON' | 'NY' | None for a UTC fractional hour (bar OPEN time)."""
    w = float(ALERT_STATE["tb_window_hours"])
    for name, start in (("LONDON", float(ALERT_STATE["tb_london_start"])),
                        ("NY", float(ALERT_STATE["tb_ny_start"]))):
        if start <= hour < start + w:
            return name
    return None


# ---------------------------------------------------------------- entry logic

def _buy_core(h, l, c, ah, al, atr, hour, bar_min):
    """
    BUY-oriented scan on numpy arrays of CLOSED bars (last element = signal bar).
    `ah` = level to break, `al` = liquidity level to sweep. SELL is handled by the
    caller mirroring prices, so this logic is written once.
    """
    buf = atr * ALERT_STATE["tb_sl_buffer_atr"]
    n = len(c)

    # A) Sweep of the Asian low, then BOS above the minor high that led into the sweep
    if ALERT_STATE["tb_use_sweep_bos"]:
        bars_since_asian = int((hour - ASIAN_END_H) * 60 / bar_min) + 1
        back = min(int(ALERT_STATE["tb_sweep_max_bars"]), bars_since_asian - 1)
        lowest_s = max(1, n - 1 - back)
        pre = int(ALERT_STATE["tb_bos_pre_bars"])
        for s in range(n - 2, lowest_s - 1, -1):
            if l[s] < al and c[s] > al:                  # wick swept, closed back inside
                if c[s + 1:].min() <= al:                # sweep failed (closed back below)
                    break
                level = float(h[max(0, s - pre):s + 1].max())
                prior = c[s + 1:-1]
                if c[-1] > level and (prior.size == 0 or prior.max() <= level):
                    ext = float(l[s:].min())
                    return {"mode": "SWEEP_BOS", "structural_sl": ext - buf,
                            "level": level, "sweep_extreme": ext}
                break                                    # only the latest sweep counts

    # B) Candle close above the Asian high (fresh break)
    if ALERT_STATE["tb_use_breakout"] and c[-1] > ah and c[-2] <= ah:
        return {"mode": "BREAKOUT", "structural_sl": ah - buf, "level": float(ah), "sweep_extreme": None}
    return None


def _targets(direction, entry, structural_sl, atr, min_rrr, source):
    sign = 1 if direction == "BUY" else -1
    risk = sign * (entry - structural_sl)
    sl_source = source
    floor_risk = atr * ALERT_STATE["tb_min_sl_atr"]
    if risk < floor_risk:
        risk, sl_source = floor_risk, "ATR floor"
    if risk > atr * ALERT_STATE["tb_max_sl_atr"]:
        return None                                      # stop too wide -> skip the setup

    rr1 = max(float(ALERT_STATE["tb_tp1_rr"]), float(min_rrr))
    rr2 = max(float(ALERT_STATE["tb_tp2_rr"]), rr1 + 0.5)
    sl = entry - sign * risk
    tp1 = entry + sign * risk * rr1
    tp2 = entry + sign * risk * rr2
    return {
        "sl_price": round(sl, 2), "tp1_price": round(tp1, 2), "tp2_price": round(tp2, 2),
        "sl_dist": round(risk, 2), "tp1_dist": round(risk * rr1, 2), "tp2_dist": round(risk * rr2, 2),
        "sl_source": sl_source, "tp1_source": "Fixed RR", "rrr": round(rr1, 2),
    }


def find_trend_breakout(tb, entry, atr_val, min_rrr, fired):
    """
    tb: context dict {trend, ah, al, hour, day_key, bar_minutes, h, l, c, ...}
        (h/l/c = numpy arrays of CLOSED bars, last = signal bar)
    fired: set of dedup keys already used. Does NOT mutate it - the caller adds the key.
    Returns None or {"direction", "setup", "targets"}.
    """
    if not tb or tb.get("trend") is None or not atr_val or atr_val <= 0:
        return None
    ah, al = tb["ah"], tb["al"]
    if ah is None or al is None or not (np.isfinite(ah) and np.isfinite(al)) or ah <= al:
        return None
    session = session_of(tb["hour"])
    if session is None:
        return None

    direction = "BUY" if tb["trend"] == "UP" else "SELL"
    key = f"{tb['day_key']}|{session}|{direction}"
    if key in fired:
        return None

    h, l, c = tb["h"], tb["l"], tb["c"]
    if len(c) < 12:
        return None

    if direction == "BUY":
        core = _buy_core(h, l, c, ah, al, atr_val, tb["hour"], tb["bar_minutes"])
    else:
        core = _buy_core(-l, -h, -c, -al, -ah, atr_val, tb["hour"], tb["bar_minutes"])
        if core:
            for k in ("structural_sl", "level", "sweep_extreme"):
                if core.get(k) is not None:
                    core[k] = -core[k]
    if not core:
        return None

    src = "Sweep Extreme" if core["mode"] == "SWEEP_BOS" else "Asian Range"
    targets = _targets(direction, float(entry), core["structural_sl"], float(atr_val), min_rrr, src)
    if targets is None:
        return None

    setup = {
        "mode": core["mode"], "session": session, "key": key,
        "level": round(core["level"], 2),
        "sweep_extreme": round(core["sweep_extreme"], 2) if core["sweep_extreme"] is not None else None,
        "ah": round(float(ah), 2), "al": round(float(al), 2),
    }
    return {"direction": direction, "setup": setup, "targets": targets}


# ---------------------------------------------------------------- live context

def build_live_context(symbol, df_entry):
    """Builds the `tb` dict for the live evaluator (BLOCKING - fetches H1 from MT5)."""
    if df_entry is None or len(df_entry) < TB_TAIL + 5:
        return None
    df_h1 = fetch_candles(symbol, mt5.TIMEFRAME_H1, 400)
    if df_h1 is None or len(df_h1) < REGIME_EMA_SLOW + 30:
        return None

    reg = add_regime_columns(df_h1).iloc[-2]             # last CLOSED H1 bar
    trend = regime_trend(reg["EMA_F"], reg["EMA_S"], reg["ADX"])

    off = pd.Timedelta(seconds=int(get_broker_utc_offset_seconds()))
    t_utc = pd.DatetimeIndex(df_entry["time"] - off)     # broker time -> true UTC
    step = pd.Series(t_utc).diff().median()
    bar_min = max(1, int(round(step.total_seconds() / 60))) if pd.notna(step) else 5

    h = df_entry["high"].to_numpy(float)
    l = df_entry["low"].to_numpy(float)
    c = df_entry["close"].to_numpy(float)
    ah, al = compute_asian_levels(t_utc, h, l, bar_min)

    sig = -2                                             # last closed entry bar
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