# strategies/smc_confluence.py
"""Smart Money Concepts + multi-timeframe confluence scoring (default strategy). Pure rules."""

from config import ALERT_STATE
from utils.common import (
    signal_spec, compute_confluence_score, format_confluence_breakdown,
    calculate_targets, build_smc_reasons,
)

LABEL = "Smart Money Concepts + Multi-TF Confluence (Default)"


def is_dual(score):
    """Two legs (TP1 + TP2/BE runner) only when the confluence score clears the threshold."""
    return score >= ALERT_STATE.get("dual_entry_score_threshold", 50)


# ---------------------------------------------------------------- live

def evaluate(a):
    close_price, rsi_val, atr_val = a["close_price"], a["rsi_val"], a["atr_val"]
    entry_bullish, trend_bullish, macro_bullish = a["entry_bullish"], a["trend_bullish"], a["macro_bullish"]
    buy_th, sell_th = ALERT_STATE["rsi_buy_threshold"], ALERT_STATE["rsi_sell_threshold"]
    min_score = ALERT_STATE["min_confluence_score"]

    signals_found, watch_found = [], []

    is_oversold = rsi_val <= buy_th
    is_overbought = rsi_val >= sell_th
    buy_structure_ok = (not ALERT_STATE["require_structure_break"]) or (a["structure"] == "BULLISH_BOS")
    sell_structure_ok = (not ALERT_STATE["require_structure_break"]) or (a["structure"] == "BEARISH_BOS")
    vol_filter_hard_ok = a["vol_filter_ok"] if ALERT_STATE["require_volume_atr_filter"] else True

    # 1. BUY
    if is_oversold and (trend_bullish or macro_bullish) and buy_structure_ok and vol_filter_hard_ok:
        sr_confluence = bool(a["near_zone"] and a["near_zone"]["type"] in ("support", "mixed") and close_price >= a["near_zone"]["price"])
        score, breakdown = compute_confluence_score(
            "BUY", rsi_val, a["structure"], a["sweeps"], a["fvg"],
            a["vol_filter_ok"], a["macd_bias"], a["candle_pattern"],
            a["divergence"], sr_confluence, entry_bullish, macro_bullish
        )

        if score >= min_score and ALERT_STATE.get("last_rsi_signal") != "BUY":
            targets = calculate_targets("BUY", close_price, atr_val, a["order_block"], a["near_zone"])
            if targets["rrr"] >= ALERT_STATE.get("min_rrr", 1.3):
                ALERT_STATE["last_rsi_signal"] = "BUY"
                sl_price, tp1_price, tp2_price, rrr = targets["sl_price"], targets["tp1_price"], targets["tp2_price"], targets["rrr"]

                msg = (
                    f"🏆 **TRADING BUTLER SIGNAL ALERT** 🏆\n\n"
                    f"• **Type:** `BUY 🟢` | **Timeframe:** {a['tf_label']}\n"
                    f"📍 **Entry:** `${close_price}` | 📊 **14-ATR:** `${atr_val}`\n"
                    f"🛡️ **SL:** `${sl_price}` ({targets['sl_source']}, {targets['sl_dist']} pts)\n"
                    f"🎯 **TP1:** `${tp1_price}` ({targets['tp1_source']}) | 🎯 **TP2:** `${tp2_price}` (RRR: `1:{rrr}`)\n\n"
                    f"{format_confluence_breakdown(score, breakdown)}\n\n"
                    f"💧 **Sweep:** {'Bullish Sweep ✅' if a['sweeps']['bullish_sweep'] else 'None'}\n"
                    f"⚡ **FVG:** {'Bullish FVG ✅' if a['fvg']['bullish_fvg'] else 'None'}\n"
                    f"🧱 **Order Block:** {'Bullish OB ✅ @ $' + str(a['order_block']['ob_level']) if a['order_block']['bullish_ob'] else 'None'}"
                )
                reasons = build_smc_reasons("BUY", a, score, breakdown, targets)
                signals_found.append(signal_spec("BUY", close_price, sl_price, tp1_price, tp2_price, msg,
                                                 score=score, reasons=reasons, dual=is_dual(score)))

    # 2. SELL
    elif is_overbought and ((not trend_bullish) or (not macro_bullish)) and sell_structure_ok and vol_filter_hard_ok:
        sr_confluence = bool(a["near_zone"] and a["near_zone"]["type"] in ("resistance", "mixed") and close_price <= a["near_zone"]["price"])
        score, breakdown = compute_confluence_score(
            "SELL", rsi_val, a["structure"], a["sweeps"], a["fvg"],
            a["vol_filter_ok"], a["macd_bias"], a["candle_pattern"],
            a["divergence"], sr_confluence, entry_bullish, not macro_bullish
        )

        if score >= min_score and ALERT_STATE.get("last_rsi_signal") != "SELL":
            targets = calculate_targets("SELL", close_price, atr_val, a["order_block"], a["near_zone"])
            if targets["rrr"] >= ALERT_STATE.get("min_rrr", 1.3):
                ALERT_STATE["last_rsi_signal"] = "SELL"
                sl_price, tp1_price, tp2_price, rrr = targets["sl_price"], targets["tp1_price"], targets["tp2_price"], targets["rrr"]

                msg = (
                    f"🏆 **TRADING BUTLER SIGNAL ALERT** 🏆\n\n"
                    f"• **Type:** `SELL 🔴` | **Timeframe:** {a['tf_label']}\n"
                    f"📍 **Entry:** `${close_price}` | 📊 **14-ATR:** `${atr_val}`\n"
                    f"🛡️ **SL:** `${sl_price}` ({targets['sl_source']}, {targets['sl_dist']} pts)\n"
                    f"🎯 **TP1:** `${tp1_price}` ({targets['tp1_source']}) | 🎯 **TP2:** `${tp2_price}` (RRR: `1:{rrr}`)\n\n"
                    f"{format_confluence_breakdown(score, breakdown)}\n\n"
                    f"💧 **Sweep:** {'Bearish Sweep ✅' if a['sweeps']['bearish_sweep'] else 'None'}\n"
                    f"⚡ **FVG:** {'Bearish FVG ✅' if a['fvg']['bearish_fvg'] else 'None'}\n"
                    f"🧱 **Order Block:** {'Bearish OB ✅ @ $' + str(a['order_block']['ob_level']) if a['order_block']['bearish_ob'] else 'None'}"
                )
                reasons = build_smc_reasons("SELL", a, score, breakdown, targets)
                signals_found.append(signal_spec("SELL", close_price, sl_price, tp1_price, tp2_price, msg,
                                                 score=score, reasons=reasons, dual=is_dual(score)))

    # Reset dedup once price exits the extreme zones
    if not (is_oversold or is_overbought):
        ALERT_STATE["last_rsi_signal"] = None

    return signals_found, watch_found


# ---------------------------------------------------------------- backtest

def decide(c):
    """c: context built by core.backtester._decide_trade. Returns a trade dict or None."""
    state, full = c["state"], c["full"]
    close_price, rsi_val, atr_val = c["close_price"], c["rsi_val"], c["atr_val"]
    entry_bullish, trend_bullish, macro_bullish = c["entry_bullish"], c["trend_bullish"], c["macro_bullish"]
    near_zone, structure = c["near_zone"], c["structure"]
    min_score, min_rrr = c["min_confluence_score"], c["min_rrr"]
    buy_th, sell_th = state["rsi_buy_threshold"], state["rsi_sell_threshold"]
    tkw = dict(sl_mult=c["sl_mult"], tp1_mult=c["tp1_mult"], tp2_mult=c["tp2_mult"])

    buy_structure_ok = (not state["require_structure_break"]) or (structure == "BULLISH_BOS")
    sell_structure_ok = (not state["require_structure_break"]) or (structure == "BEARISH_BOS")

    if rsi_val <= buy_th and (trend_bullish or macro_bullish) and buy_structure_ok:
        sr_confluence = bool(near_zone and near_zone["type"] in ("support", "mixed") and close_price >= near_zone["price"])
        score, breakdown = compute_confluence_score(
            "BUY", rsi_val, structure, full.get("sweeps", {}), full.get("fvg", {}), full["vol_filter_ok"],
            full["macd_bias"], full["candle_pattern"], full["divergence"], sr_confluence,
            entry_bullish, macro_bullish
        )
        if score >= min_score and state.get("last_signal") != "BUY":
            targets = calculate_targets("BUY", close_price, atr_val, full.get("order_block", {}), near_zone, **tkw)
            if targets["rrr"] >= min_rrr:
                state["last_signal"] = "BUY"
                return {"direction": "BUY", "entry": close_price, "score": score, "breakdown": breakdown, **targets}

    elif rsi_val >= sell_th and ((not trend_bullish) or (not macro_bullish)) and sell_structure_ok:
        sr_confluence = bool(near_zone and near_zone["type"] in ("resistance", "mixed") and close_price <= near_zone["price"])
        score, breakdown = compute_confluence_score(
            "SELL", rsi_val, structure, full.get("sweeps", {}), full.get("fvg", {}), full["vol_filter_ok"],
            full["macd_bias"], full["candle_pattern"], full["divergence"], sr_confluence,
            entry_bullish, not macro_bullish
        )
        if score >= min_score and state.get("last_signal") != "SELL":
            targets = calculate_targets("SELL", close_price, atr_val, full.get("order_block", {}), near_zone, **tkw)
            if targets["rrr"] >= min_rrr:
                state["last_signal"] = "SELL"
                return {"direction": "SELL", "entry": close_price, "score": score, "breakdown": breakdown, **targets}

    # Mirror live: only clear dedup once RSI has left the extreme zone
    if not (rsi_val <= buy_th or rsi_val >= sell_th):
        state["last_signal"] = None
    return None