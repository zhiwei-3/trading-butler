"""PURE entry rules. Arrays are CLOSED bars, last element = signal bar. Shared by live and backtest."""

import numpy as np

SCORE = 65


def squeeze_run_len(sq):
    """Number of consecutive squeeze bars ending at the last bar."""
    n = 0
    for v in sq[::-1]:
        if not v:
            break
        n += 1
    return n


def build_targets(direction, entry, structural_sl, atr, min_rrr, source, p):
    sign = 1 if direction == "BUY" else -1
    risk = sign * (entry - structural_sl)
    sl_source = source
    floor = atr * float(p["min_sl_atr"])
    if risk < floor:
        risk, sl_source = floor, "ATR floor"
    if risk > atr * float(p["max_sl_atr"]):
        return None                                   # stop too wide -> skip the setup
    rr1 = max(float(p["tp1_rr"]), float(min_rrr))
    rr2 = max(float(p["tp2_rr"]), rr1 + 0.5)
    return {
        "sl_price": round(entry - sign * risk, 2),
        "tp1_price": round(entry + sign * risk * rr1, 2),
        "tp2_price": round(entry + sign * risk * rr2, 2),
        "sl_dist": round(risk, 2), "tp1_dist": round(risk * rr1, 2), "tp2_dist": round(risk * rr2, 2),
        "sl_source": sl_source, "tp1_source": "Fixed RR", "rrr": round(rr1, 2),
    }


def current_box(ctx, p):
    """Box of the squeeze run ending at the LAST closed bar (used by the heads-up).
    Returns None or {"lo", "hi", "bars", "start_key"}."""
    sq, h, l, t = ctx["sq"], ctx["h"], ctx["l"], ctx["t"]
    n = len(sq)
    run = squeeze_run_len(sq)
    if run < int(p["min_bars"]):
        return None
    start = n - run
    use = max(start, n - int(p["box_max_bars"]))
    return {"lo": float(l[use:].min()), "hi": float(h[use:].max()),
            "bars": run, "start_key": str(t[start])}


def find_compression_breakout(ctx, entry, atr, min_rrr, p, fired):
    """
    ctx: {"o","h","l","c": arrays, "sq": bool array, "t": times, "hour": float, "ema_bull": bool}.
    The squeeze run must end at the bar BEFORE the signal bar; the signal bar must close outside its box.
    Does NOT mutate `fired` - the caller adds the key.
    Returns None or {"direction", "key", "level", "range", "squeeze_bars", "targets"}.
    """
    if not ctx or not atr or atr <= 0:
        return None
    sq, o, h, l, c = ctx["sq"], ctx["o"], ctx["h"], ctx["l"], ctx["c"]
    n = len(c)
    if n < 10 or len(sq) != n:
        return None

    if p["use_session_filter"] and not (float(p["session_start"]) <= ctx["hour"] < float(p["session_end"])):
        return None

    # --- compression run ending at the prior bar
    end = n - 2
    if not sq[end]:
        return None
    start = end
    while start > 0 and sq[start - 1]:
        start -= 1
    run_len = end - start + 1
    if run_len < int(p["min_bars"]):
        return None
    box_start = max(start, end - int(p["box_max_bars"]) + 1)

    hi, lo = float(h[box_start:end + 1].max()), float(l[box_start:end + 1].min())
    width = hi - lo
    if width <= 0 or width > atr * float(p["max_box_atr"]):
        return None

    # --- decisive close outside the box (acceptance, not a wick poke)
    rng = float(h[-1] - l[-1])
    if rng <= 0:
        return None
    body = float(c[-1] - o[-1])
    if abs(body) / rng < float(p["min_body_ratio"]):
        return None
    margin = atr * float(p["close_margin_atr"])
    if c[-1] > hi + margin and body > 0:
        direction, level = "BUY", hi
    elif c[-1] < lo - margin and body < 0:
        direction, level = "SELL", lo
    else:
        return None

    if p["ema_filter"] and bool(ctx["ema_bull"]) != (direction == "BUY"):
        return None

    key = f"{ctx['t'][start]}|{direction}"
    if key in fired:
        return None

    # --- stop: opposite box edge (or midpoint) +/- ATR buffer
    buf = atr * float(p["sl_buffer_atr"])
    if p["sl_at"] == "mid":
        anchor, source = (hi + lo) / 2.0, "Box mid"
    else:
        anchor, source = (lo if direction == "BUY" else hi), "Box edge"
    structural_sl = anchor - buf if direction == "BUY" else anchor + buf

    targets = build_targets(direction, float(entry), structural_sl, float(atr), min_rrr, source, p)
    if targets is None:
        return None
    return {"direction": direction, "key": key, "level": round(level, 2),
            "range": (round(lo, 2), round(hi, 2)), "squeeze_bars": run_len, "targets": targets}