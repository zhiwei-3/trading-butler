# strategies/_template/rules.py
"""PURE rules, shared by live and backtest. Arrays are CLOSED bars, last element = signal bar."""


def build_targets(direction, entry, structural_sl, atr, min_rrr, p):
    sign = 1 if direction == "BUY" else -1
    risk = sign * (entry - structural_sl)
    source = "Range edge"
    floor = atr * float(p["min_sl_atr"])
    if risk < floor:
        risk, source = floor, "ATR floor"
    if risk > atr * float(p["max_sl_atr"]):
        return None                                   # stop too wide -> skip the setup
    rr1 = max(float(p["tp1_rr"]), float(min_rrr))
    rr2 = max(float(p["tp2_rr"]), rr1 + 0.5)
    return {
        "sl_price": round(entry - sign * risk, 2),
        "tp1_price": round(entry + sign * risk * rr1, 2),
        "tp2_price": round(entry + sign * risk * rr2, 2),
        "sl_dist": round(risk, 2), "tp1_dist": round(risk * rr1, 2), "tp2_dist": round(risk * rr2, 2),
        "sl_source": source, "tp1_source": "Fixed RR", "rrr": round(rr1, 2),
    }


def find_breakout(ctx, entry, atr, min_rrr, p, fired):
    """ctx: {"h","l","c": numpy arrays, "bar_id": str}. Does NOT mutate `fired` - the caller adds the key."""
    if not ctx or not atr or atr <= 0:
        return None
    n = int(p["lookback"])
    h, l, c = ctx["h"], ctx["l"], ctx["c"]
    if len(c) < n + 2:
        return None

    hi, lo = float(h[-1 - n:-1].max()), float(l[-1 - n:-1].min())              # range before the signal bar
    hi_prev, lo_prev = float(h[-2 - n:-2].max()), float(l[-2 - n:-2].min())    # range before the bar prior (freshness)
    buf = atr * float(p["sl_buffer_atr"])

    if c[-1] > hi and c[-2] <= hi_prev:
        direction, structural_sl, level = "BUY", lo - buf, hi
    elif c[-1] < lo and c[-2] >= lo_prev:
        direction, structural_sl, level = "SELL", hi + buf, lo
    else:
        return None

    key = f"{ctx['bar_id']}|{direction}"
    if key in fired:
        return None
    targets = build_targets(direction, float(entry), structural_sl, float(atr), min_rrr, p)
    if targets is None:
        return None
    return {"direction": direction, "key": key, "level": round(level, 2),
            "range": (round(lo, 2), round(hi, 2)), "targets": targets}