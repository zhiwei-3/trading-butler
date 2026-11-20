# utils/common.py
"""Pure helpers shared by strategies and the evaluator: targets, reasoning text, signal spec.
No I/O of any kind (no MT5, DB, Telegram, charts) and no settings access."""


def signal_spec(direction, close_price, sl_price, tp1_price, tp2_price, base_msg, score=0,
                zone=None, reasons=None, risk=None, dual=False):
    """What a strategy hands back to the evaluator. core.evaluator.package_signal(a, **spec)
    does the side effects (DB log, execution intent, chart). risk: {"risk_pct", "max_lot"} or None."""
    return {
        "direction": direction, "close_price": close_price,
        "sl_price": sl_price, "tp1_price": tp1_price, "tp2_price": tp2_price,
        "base_msg": base_msg, "score": score, "zone": zone, "reasons": reasons,
        "risk": risk, "dual": dual,
    }


def calculate_targets(direction, close_price, atr_val, order_block, near_zone, sl_mult, tp1_mult, tp2_mult):
    min_dist = atr_val * 0.5
    ob_buffer = atr_val * 0.25

    if direction == "BUY":
        sl_price, sl_source = close_price - atr_val * sl_mult, "ATR"
        ob_level = order_block.get("bullish_ob_level") if order_block.get("bullish_ob") else None
        if ob_level is not None:
            ob_sl = ob_level - ob_buffer
            if ob_sl < close_price and (close_price - ob_sl) >= min_dist:
                sl_price, sl_source = ob_sl, "Order Block"

        tp1_price, tp1_source = close_price + atr_val * tp1_mult, "ATR"
        if near_zone and near_zone["type"] in ("resistance", "mixed") and near_zone["price"] > close_price and (near_zone["price"] - close_price) >= min_dist:
            tp1_price, tp1_source = near_zone["price"], "S/R Zone"

        tp2_price = max(close_price + atr_val * tp2_mult, tp1_price + atr_val * 0.5)

    else:  # SELL
        sl_price, sl_source = close_price + atr_val * sl_mult, "ATR"
        ob_level = order_block.get("bearish_ob_level") if order_block.get("bearish_ob") else None
        if ob_level is not None:
            ob_sl = ob_level + ob_buffer
            if ob_sl > close_price and (ob_sl - close_price) >= min_dist:
                sl_price, sl_source = ob_sl, "Order Block"

        tp1_price, tp1_source = close_price - atr_val * tp1_mult, "ATR"
        if near_zone and near_zone["type"] in ("support", "mixed") and near_zone["price"] < close_price and (close_price - near_zone["price"]) >= min_dist:
            tp1_price, tp1_source = near_zone["price"], "S/R Zone"

        tp2_price = min(close_price - atr_val * tp2_mult, tp1_price - atr_val * 0.5)

    sl_dist = abs(close_price - sl_price)
    tp1_dist = abs(tp1_price - close_price)
    tp2_dist = abs(tp2_price - close_price)
    rrr = round(tp1_dist / sl_dist, 2) if sl_dist > 0 else 0

    return {
        "sl_price": round(sl_price, 2), "tp1_price": round(tp1_price, 2), "tp2_price": round(tp2_price, 2),
        "sl_dist": round(sl_dist, 2), "tp1_dist": round(tp1_dist, 2), "tp2_dist": round(tp2_dist, 2),
        "sl_source": sl_source, "tp1_source": tp1_source, "rrr": rrr,
    }


def target_reasons(t):
    return [f"SL set by {t['sl_source']} ({t['sl_dist']} pts); TP1 by {t['tp1_source']}; RRR 1:{t['rrr']}"]


def format_reasons(reasons):
    if not reasons:
        return ""
    return "🧠 **Why this trade:**\n" + "\n".join(f"  • {r}" for r in reasons)