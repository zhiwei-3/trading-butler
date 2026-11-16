"""PURE entry rules. Arrays are CLOSED bars, last element = signal bar. Shared by live and backtest."""

import numpy as np

from .sessions import ASIAN_END_H, session_of

SCORE = 70


def _buy_core(h, l, c, ah, al, atr, hour, bar_min, p):
    """
    BUY-oriented scan. `ah` = level to break, `al` = liquidity level to sweep. SELL is handled by the
    caller mirroring prices, so this logic is written once.
    """
    buf = atr * p["sl_buffer_atr"]
    n = len(c)

    # A) Sweep of the Asian low, then BOS above the minor high that led into the sweep
    if p["use_sweep_bos"]:
        bars_since_asian = int((hour - ASIAN_END_H) * 60 / bar_min) + 1
        back = min(int(p["sweep_max_bars"]), bars_since_asian - 1)
        lowest_s = max(1, n - 1 - back)
        pre = int(p["bos_pre_bars"])
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
    if p["use_breakout"] and c[-1] > ah and c[-2] <= ah:
        return {"mode": "BREAKOUT", "structural_sl": ah - buf, "level": float(ah), "sweep_extreme": None}
    return None


def _targets(direction, entry, structural_sl, atr, min_rrr, source, p):
    sign = 1 if direction == "BUY" else -1
    risk = sign * (entry - structural_sl)
    sl_source = source
    floor_risk = atr * p["min_sl_atr"]
    if risk < floor_risk:
        risk, sl_source = floor_risk, "ATR floor"
    if risk > atr * p["max_sl_atr"]:
        return None                                      # stop too wide -> skip the setup

    rr1 = max(float(p["tp1_rr"]), float(min_rrr))
    rr2 = max(float(p["tp2_rr"]), rr1 + 0.5)
    sl = entry - sign * risk
    tp1 = entry + sign * risk * rr1
    tp2 = entry + sign * risk * rr2
    return {
        "sl_price": round(sl, 2), "tp1_price": round(tp1, 2), "tp2_price": round(tp2, 2),
        "sl_dist": round(risk, 2), "tp1_dist": round(risk * rr1, 2), "tp2_dist": round(risk * rr2, 2),
        "sl_source": sl_source, "tp1_source": "Fixed RR", "rrr": round(rr1, 2),
    }


def find_trend_breakout(tb, entry, atr_val, min_rrr, p, fired):
    """
    tb: context {trend, ah, al, hour, day_key, bar_minutes, h, l, c, ...} (see connectors).
    fired: set of dedup keys already used. Does NOT mutate it - the caller adds the key.
    Returns None or {"direction", "setup", "targets"}.
    """
    if not tb or tb.get("trend") is None or not atr_val or atr_val <= 0:
        return None
    ah, al = tb["ah"], tb["al"]
    if ah is None or al is None or not (np.isfinite(ah) and np.isfinite(al)) or ah <= al:
        return None
    session = session_of(tb["hour"], p)
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
        core = _buy_core(h, l, c, ah, al, atr_val, tb["hour"], tb["bar_minutes"], p)
    else:
        core = _buy_core(-l, -h, -c, -al, -ah, atr_val, tb["hour"], tb["bar_minutes"], p)
        if core:
            for k in ("structural_sl", "level", "sweep_extreme"):
                if core.get(k) is not None:
                    core[k] = -core[k]
    if not core:
        return None

    src = "Sweep Extreme" if core["mode"] == "SWEEP_BOS" else "Asian Range"
    targets = _targets(direction, float(entry), core["structural_sl"], float(atr_val), min_rrr, src, p)
    if targets is None:
        return None

    setup = {
        "mode": core["mode"], "session": session, "key": key,
        "level": round(core["level"], 2),
        "sweep_extreme": round(core["sweep_extreme"], 2) if core["sweep_extreme"] is not None else None,
        "ah": round(float(ah), 2), "al": round(float(al), 2),
    }
    return {"direction": direction, "setup": setup, "targets": targets}