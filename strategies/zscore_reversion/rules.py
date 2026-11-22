"""PURE entry rules. Arrays are CLOSED bars, last element = signal bar. Shared by live and backtest."""

import numpy as np

SCORE = 60


def _finite(*vals):
    return all(np.isfinite(v) for v in vals)


def regime_ok(ctx, atr, p):
    """Range-bound market: low ADX AND a flat mean. False when any input is missing."""
    if not ctx or not atr or atr <= 0:
        return False
    adx, slope = ctx["adx"], ctx["slope"]
    if not _finite(adx, slope):
        return False
    return adx <= float(p["adx_max"]) and abs(slope) <= float(p["slope_max_atr"]) * atr


def build_targets(direction, entry, structural_sl, sma, sd, atr, min_rrr, source, p):
    """TP1 = the rolling mean. RR is whatever the setup offers; below min_rrr -> skip."""
    sign = 1 if direction == "BUY" else -1
    risk = sign * (entry - structural_sl)
    sl_source = source
    floor = atr * float(p["min_sl_atr"])
    if risk < floor:
        risk, sl_source = floor, "ATR floor"
    if risk > atr * float(p["max_sl_atr"]):
        return None                                   # stop too wide -> skip the setup

    tp1_dist = sign * (sma - entry)
    if tp1_dist <= 0:
        return None                                   # price already at/through the mean
    rr1 = tp1_dist / risk
    if rr1 < float(min_rrr):
        return None

    tp1 = sma
    tp2 = tp1 + sign * max(float(p["tp2_z"]) * sd, atr * 0.5)
    tp2_dist = sign * (tp2 - entry)
    return {
        "sl_price": round(entry - sign * risk, 2),
        "tp1_price": round(tp1, 2),
        "tp2_price": round(tp2, 2),
        "sl_dist": round(risk, 2), "tp1_dist": round(tp1_dist, 2), "tp2_dist": round(tp2_dist, 2),
        "sl_source": sl_source, "tp1_source": "Mean (Z=0)", "rrr": round(rr1, 2),
    }


def find_zscore_reversion(ctx, entry, atr, min_rrr, p, fired):
    """
    ctx: {"o","h","l","c","z","t": arrays (last = signal bar), "sma","sd","adx","slope": floats}.
    Prior bar reached the extreme, the signal bar hooks back toward the mean. Does NOT mutate `fired`.
    Returns None or {"direction", "key", "z", "z_prev", "mean", "targets"}.
    """
    if not ctx or not atr or atr <= 0:
        return None
    z, o, h, l, c = ctx["z"], ctx["o"], ctx["h"], ctx["l"], ctx["c"]
    if len(z) < 3:
        return None
    z1, z0 = float(z[-1]), float(z[-2])
    sma, sd = float(ctx["sma"]), float(ctx["sd"])
    if not _finite(z1, z0, sma, sd) or sd <= 0:
        return None
    if not regime_ok(ctx, atr, p):
        return None

    ez, sz = float(p["entry_z"]), float(p["stop_z"])
    if -sz < z0 <= -ez and z0 < z1 < 0:
        direction = "BUY"
    elif ez <= z0 < sz and 0 < z1 < z0:
        direction = "SELL"
    else:
        return None

    if p["require_candle"]:
        if direction == "BUY" and not c[-1] > o[-1]:
            return None
        if direction == "SELL" and not c[-1] < o[-1]:
            return None

    key = f"{ctx['t'][-1]}|{direction}"
    if key in fired:
        return None

    # --- hard stop beyond the extreme
    buf = atr * float(p["sl_buffer_atr"])
    k = int(p["extreme_bars"])
    if direction == "BUY":
        if p["sl_mode"] == "extreme":
            level, source = float(l[-k:].min()), "Recent extreme"
        else:
            level, source = sma - sz * sd, f"Z=-{sz:g}"
        structural_sl = level - buf
    else:
        if p["sl_mode"] == "extreme":
            level, source = float(h[-k:].max()), "Recent extreme"
        else:
            level, source = sma + sz * sd, f"Z=+{sz:g}"
        structural_sl = level + buf

    targets = build_targets(direction, float(entry), structural_sl, sma, sd, float(atr), min_rrr, source, p)
    if targets is None:
        return None
    return {"direction": direction, "key": key, "z": round(z1, 2), "z_prev": round(z0, 2),
            "mean": round(sma, 2), "targets": targets}