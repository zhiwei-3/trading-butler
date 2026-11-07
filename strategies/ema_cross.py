# strategies/ema_cross.py
"""EMA20/EMA50 crossover on the entry timeframe. Pure rules."""

from config import ALERT_STATE
from utils.common import signal_spec, calculate_targets, target_reasons, simple_breakdown

LABEL = "Triple EMA Trend Crossover + Volatility Filter"


def is_dual(score):
    return False   # always a single TP1-only leg


# ---------------------------------------------------------------- live

def evaluate(a):
    df = a["df_entry"]
    close_price, atr_val = a["close_price"], a["atr_val"]
    ema20, ema50 = df['EMA_20'].iloc[-1], df['EMA_50'].iloc[-1]
    prev_ema20, prev_ema50 = df['EMA_20'].iloc[-2], df['EMA_50'].iloc[-2]

    signals, watches = [], []

    bullish_cross = (prev_ema20 <= prev_ema50 and ema20 > ema50)
    bearish_cross = (prev_ema20 >= prev_ema50 and ema20 < ema50)

    for direction, crossed, word in (("BUY", bullish_cross, "above"), ("SELL", bearish_cross, "below")):
        if not crossed or ALERT_STATE["last_rsi_signal"] == direction:
            continue
        targets = calculate_targets(direction, close_price, atr_val, a["order_block"], a["near_zone"])
        if targets["rrr"] >= ALERT_STATE.get("min_rrr", 1.0):
            ALERT_STATE["last_rsi_signal"] = direction
            icon = "🟢" if direction == "BUY" else "🔴"
            base_msg = (f"🏆 **EMA CROSS {direction} ALERT** {icon}\n\n📍 **Entry:** `${close_price}` | "
                        f"🛡️ **SL:** `${targets['sl_price']}` | 🎯 **TP1:** `${targets['tp1_price']}`")
            reasons = [
                f"EMA20 ({ema20:.2f}) crossed {word} EMA50 ({ema50:.2f}) on the entry timeframe",
                f"Higher-timeframe context: trend {'bullish' if a['trend_bullish'] else 'bearish'}, "
                f"macro {'bullish' if a['macro_bullish'] else 'bearish'}",
            ] + target_reasons(targets)
            signals.append(signal_spec(direction, close_price, targets["sl_price"], targets["tp1_price"],
                                       targets["tp2_price"], base_msg, score=70, reasons=reasons,
                                       dual=is_dual(70)))
        break   # a bullish and bearish cross can't both be true

    if not (bullish_cross or bearish_cross):
        ALERT_STATE["last_rsi_signal"] = None

    return signals, watches


# ---------------------------------------------------------------- backtest

def decide(c):
    state, full = c["state"], c["full"]
    close_price, atr_val = c["close_price"], c["atr_val"]
    entry_bullish, macro_bullish = c["entry_bullish"], c["macro_bullish"]
    near_zone, structure, min_rrr = c["near_zone"], c["structure"], c["min_rrr"]
    order_block = full.get("order_block", {})
    tkw = dict(sl_mult=c["sl_mult"], tp1_mult=c["tp1_mult"], tp2_mult=c["tp2_mult"])

    prev_bearish = state.get("prev_ema_bearish", False)
    prev_bullish = state.get("prev_ema_bullish", False)

    if entry_bullish and prev_bearish and state.get("last_signal") != "BUY":
        targets = calculate_targets("BUY", close_price, atr_val, order_block, near_zone, **tkw)
        if targets["rrr"] >= min_rrr:
            state["last_signal"] = "BUY"
            return {"direction": "BUY", "entry": close_price, "score": 70,
                    "breakdown": simple_breakdown(True, close_price, near_zone, structure, entry_bullish, macro_bullish),
                    **targets}

    elif (not entry_bullish) and prev_bullish and state.get("last_signal") != "SELL":
        targets = calculate_targets("SELL", close_price, atr_val, order_block, near_zone, **tkw)
        if targets["rrr"] >= min_rrr:
            state["last_signal"] = "SELL"
            return {"direction": "SELL", "entry": close_price, "score": 70,
                    "breakdown": simple_breakdown(False, close_price, near_zone, structure, entry_bullish, macro_bullish),
                    **targets}

    state["last_signal"] = None
    return None