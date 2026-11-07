# strategies/rsi_reversion.py
"""RSI overbought/oversold reversion at S/R zones. Pure rules."""

from config import ALERT_STATE
from utils.common import signal_spec, calculate_targets, target_reasons, simple_breakdown

LABEL = "RSI Overbought/Oversold S/R Zone Reversion"


def is_dual(score):
    return False   # always a single TP1-only leg


# ---------------------------------------------------------------- live

def evaluate(a):
    close_price, rsi_val, atr_val = a["close_price"], a["rsi_val"], a["atr_val"]
    near_zone, order_block = a["near_zone"], a["order_block"]

    buy_th = ALERT_STATE.get("rsi_buy_threshold", 30)
    sell_th = ALERT_STATE.get("rsi_sell_threshold", 70)

    signals, watches = [], []

    bullish_rev = (
        rsi_val <= buy_th and
        near_zone and near_zone["type"] in ("support", "mixed") and
        close_price >= near_zone["price"]
    )
    bearish_rev = (
        rsi_val >= sell_th and
        near_zone and near_zone["type"] in ("resistance", "mixed") and
        close_price <= near_zone["price"]
    )

    for direction, triggered in (("BUY", bullish_rev), ("SELL", bearish_rev)):
        if not triggered or ALERT_STATE["last_rsi_signal"] == direction:
            continue
        targets = calculate_targets(direction, close_price, atr_val, order_block, near_zone)
        if targets["rrr"] >= ALERT_STATE.get("min_rrr", 1.0):
            ALERT_STATE["last_rsi_signal"] = direction
            is_buy = direction == "BUY"
            icon = "🟢" if is_buy else "🔴"
            base_msg = (f"🔄 **RSI REVERSION {direction} ALERT** {icon}\n\n📍 **Entry:** `${close_price}` | "
                        f"🛡️ **SL:** `${targets['sl_price']}` | 🎯 **TP1:** `${targets['tp1_price']}`")
            reasons = [
                f"RSI {rsi_val} is {'oversold' if is_buy else 'overbought'} (trigger {'<=' if is_buy else '>='} "
                f"{buy_th if is_buy else sell_th})",
                f"Price is {'holding above' if is_buy else 'rejecting below'} a {near_zone['type']} zone at "
                f"{near_zone['price']} ({near_zone['touches']} touches)",
            ] + target_reasons(targets)
            signals.append(signal_spec(direction, close_price, targets["sl_price"], targets["tp1_price"],
                                       targets["tp2_price"], base_msg, score=75, reasons=reasons,
                                       dual=is_dual(75)))
        break

    if not (bullish_rev or bearish_rev):
        ALERT_STATE["last_rsi_signal"] = None

    return signals, watches


# ---------------------------------------------------------------- backtest

def decide(c):
    state, full = c["state"], c["full"]
    close_price, rsi_val, atr_val = c["close_price"], c["rsi_val"], c["atr_val"]
    entry_bullish, macro_bullish = c["entry_bullish"], c["macro_bullish"]
    near_zone, structure, min_rrr = c["near_zone"], c["structure"], c["min_rrr"]
    order_block = full.get("order_block", {})
    buy_th, sell_th = state["rsi_buy_threshold"], state["rsi_sell_threshold"]
    tkw = dict(sl_mult=c["sl_mult"], tp1_mult=c["tp1_mult"], tp2_mult=c["tp2_mult"])

    bullish_rev = (rsi_val <= buy_th and near_zone and near_zone["type"] in ("support", "mixed") and close_price >= near_zone["price"])
    bearish_rev = (rsi_val >= sell_th and near_zone and near_zone["type"] in ("resistance", "mixed") and close_price <= near_zone["price"])

    if bullish_rev and state.get("last_signal") != "BUY":
        targets = calculate_targets("BUY", close_price, atr_val, order_block, near_zone, **tkw)
        if targets["rrr"] >= min_rrr:
            state["last_signal"] = "BUY"
            return {"direction": "BUY", "entry": close_price, "score": 75,
                    "breakdown": simple_breakdown(True, close_price, near_zone, structure, entry_bullish, macro_bullish),
                    **targets}

    elif bearish_rev and state.get("last_signal") != "SELL":
        targets = calculate_targets("SELL", close_price, atr_val, order_block, near_zone, **tkw)
        if targets["rrr"] >= min_rrr:
            state["last_signal"] = "SELL"
            return {"direction": "SELL", "entry": close_price, "score": 75,
                    "breakdown": simple_breakdown(False, close_price, near_zone, structure, entry_bullish, macro_bullish),
                    **targets}

    if not (bullish_rev or bearish_rev):
        state["last_signal"] = None
    return None