# strategies/trend_breakout.py
"""
Systematic Trend Following / Volatility Breakout — self-contained, PURE rules.

Regime  : H1 EMA50 vs EMA200 + ADX(14) > tb_adx_min  (last CLOSED H1 bar)
Entry   : inside the London / NY open window, in the regime direction
            SWEEP_BOS : Asian high/low swept, then a BOS in the trend direction
            BREAKOUT  : candle CLOSE beyond the Asian range
Targets : structural SL (+ fractional ATR buffer, ATR floor/cap) and fixed-RR TPs

All time logic is TRUE UTC (broker-server time minus the detected offset).
No data fetching here: core.evaluator / core.backtester fetch H1 candles and the broker
offset and pass them in.
"""

import numpy as np
import pandas as pd
import pandas_ta as ta

from config import ALERT_STATE
from utils.common import signal_spec, target_reasons

LABEL = "H1 Trend/ADX Regime + Asian Range Breakout / Sweep+BOS (ATR-risk sizing)"
LIVE_ENTRY_BARS = 400      # analyze_market fetches this many entry-TF candles for this strategy

ASIAN_START_H = 0.0
ASIAN_END_H = 7.0
TB_TAIL = 60                       # closed bars handed to the entry logic
REGIME_EMA_FAST, REGIME_EMA_SLOW, REGIME_ADX_LEN = 50, 200, 14


def is_dual(score):
    return bool(ALERT_STATE.get("tb_dual_entry", True))


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


# ---------------------------------------------------------------- live context (hook; data passed in by core.evaluator)

def build_live_context(df_entry, df_h1, broker_offset_s):
    """Builds the `tb` dict for the live evaluator. Pure: candles + broker offset come in as arguments."""
    if df_entry is None or len(df_entry) < TB_TAIL + 5:
        return None
    if df_h1 is None or len(df_h1) < REGIME_EMA_SLOW + 30:
        return None

    reg = add_regime_columns(df_h1).iloc[-2]             # last CLOSED H1 bar
    trend = regime_trend(reg["EMA_F"], reg["EMA_S"], reg["ADX"])

    off = pd.Timedelta(seconds=int(broker_offset_s))
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


# ---------------------------------------------------------------- live strategy

def evaluate(a):
    tb = a.get("tb")
    if not tb:
        return [], []

    # Dedup keys are per-day; drop yesterday's.
    ALERT_STATE["tb_fired"] = {k for k in ALERT_STATE.get("tb_fired", set()) if k.startswith(tb["day_key"])}
    found = find_trend_breakout(tb, a["close_price"], a["atr_val"],
                                ALERT_STATE.get("min_rrr", 1.5), ALERT_STATE["tb_fired"])
    if not found:
        return [], []

    direction, setup, t = found["direction"], found["setup"], found["targets"]
    ALERT_STATE["tb_fired"].add(setup["key"])

    is_buy = direction == "BUY"
    icon = "🟢" if is_buy else "🔴"
    side_word = "above" if is_buy else "below"
    mode_txt = "Sweep + BOS" if setup["mode"] == "SWEEP_BOS" else "Range Breakout"
    close_price = a["close_price"]

    msg = (
        f"🌅 **TREND BREAKOUT {direction} ALERT** {icon}\n\n"
        f"📍 **Entry:** `${close_price}` | 📊 **14-ATR:** `${a['atr_val']}`\n"
        f"🛡️ **SL:** `${t['sl_price']}` ({t['sl_source']}, {t['sl_dist']} pts)\n"
        f"🎯 **TP1:** `${t['tp1_price']}` | 🎯 **TP2:** `${t['tp2_price']}` (RRR: `1:{t['rrr']}`)\n\n"
        f"🧭 **H1 Regime:** {'Uptrend' if is_buy else 'Downtrend'} (ADX `{tb['adx']:.1f}`)\n"
        f"🌏 **Asian Range:** `${setup['al']} – ${setup['ah']}` | **Setup:** {mode_txt} | **Session:** {setup['session']}"
    )

    swept = "Asian low" if is_buy else "Asian high"
    brk = "Asian high" if is_buy else "Asian low"
    reasons = [
        f"H1 {'uptrend' if is_buy else 'downtrend'}: EMA50 {'>' if is_buy else '<'} EMA200 "
        f"({tb['ema_f']:.2f} / {tb['ema_s']:.2f}), ADX {tb['adx']:.1f} > {ALERT_STATE['tb_adx_min']}",
        f"{setup['session']} open window, Asian range {setup['al']} - {setup['ah']}",
    ]
    if setup["mode"] == "SWEEP_BOS":
        reasons.append(f"{swept} was swept (wick to {setup['sweep_extreme']}) and price closed back inside")
        reasons.append(f"Break of structure: closed {side_word} {setup['level']} in the H1 trend direction")
    else:
        reasons.append(f"Candle closed {side_word} the {brk} ({setup['level']}) in the H1 trend direction")
    reasons += target_reasons(t)

    return [signal_spec(direction, close_price, t["sl_price"], t["tp1_price"], t["tp2_price"], msg,
                        score=70, zone={"fvg_top": setup["ah"], "fvg_bottom": setup["al"]},
                        reasons=reasons, dynamic_sizing=True, dual=is_dual(70))], []


# ---------------------------------------------------------------- backtest

def decide(c):
    state, full = c["state"], c["full"]
    found = find_trend_breakout(full.get("tb"), c["close_price"], c["atr_val"], c["min_rrr"], state["tb_fired"])
    if not found:
        return None
    state["tb_fired"].add(found["setup"]["key"])
    return {"direction": found["direction"], "entry": c["close_price"], "score": 70,
            "breakdown": [], **found["targets"]}