# utils/common.py
"""Pure helpers shared by strategies and the evaluator: scoring, targets, reasoning text, signal spec.
No I/O of any kind (no MT5, DB, Telegram, charts)."""

from config import ALERT_STATE, CONFLUENCE_WEIGHTS


_FACTOR_TEXT = {
    "EMA Trend Align": "Entry-timeframe EMA20/EMA50 trend agrees with the trade direction",
    "Macro Trend Align": "Macro-timeframe trend agrees with the trade direction",
    "Structure BOS": "Fresh break of structure in the trade direction",
    "Liquidity Sweep": "Liquidity swept on the latest candle and price closed back inside",
    "Fair Value Gap": "Fresh fair value gap in the trade direction",
    "Volume/ATR Activity": "ATR and tick volume above their recent averages",
    "MACD Confirmation": "MACD bias confirms the direction",
    "Candlestick Pattern": "Confirming candlestick pattern on entry",
    "RSI Divergence": "RSI divergence at the last two swings",
    "Support/Resistance": "Price is sitting on a matching support/resistance zone",
}


# ---------------------------------------------------------------- signal spec

def signal_spec(direction, close_price, sl_price, tp1_price, tp2_price, base_msg, score=0,
                zone=None, reasons=None, dynamic_sizing=False, dual=False):
    """What a strategy hands back to the evaluator. core.evaluator.package_signal(a, **spec)
    does the side effects (DB log, execution intent, chart)."""
    return {
        "direction": direction, "close_price": close_price,
        "sl_price": sl_price, "tp1_price": tp1_price, "tp2_price": tp2_price,
        "base_msg": base_msg, "score": score, "zone": zone, "reasons": reasons,
        "dynamic_sizing": dynamic_sizing, "dual": dual,
    }


# ---------------------------------------------------------------- confluence scoring

def score_label(score):
    if score >= 80: return "🔥 Very Strong"
    if score >= 65: return "✅ Strong"
    if score >= 50: return "⚠️ Moderate"
    return "❌ Weak"


def compute_confluence_score(direction, rsi_val, structure, sweeps, fvg, vol_filter_ok, macd_bias,
                             candle_pattern, divergence, sr_confluence, entry_bullish, macro_aligned):
    breakdown = []

    rsi_pts = CONFLUENCE_WEIGHTS["rsi_zone"] if (rsi_val <= 20 if direction == "BUY" else rsi_val >= 80) else (10 if (rsi_val <= 25 if direction == "BUY" else rsi_val >= 75) else 6)
    breakdown.append(("RSI Zone", rsi_pts, CONFLUENCE_WEIGHTS["rsi_zone"]))

    ema_match = (direction == "BUY" and entry_bullish) or (direction == "SELL" and not entry_bullish)
    breakdown.append(("EMA Trend Align", CONFLUENCE_WEIGHTS["ema_trend"] if ema_match else 0, CONFLUENCE_WEIGHTS["ema_trend"]))

    breakdown.append(("Macro Trend Align", CONFLUENCE_WEIGHTS["macro_trend"] if macro_aligned else 0, CONFLUENCE_WEIGHTS["macro_trend"]))

    bos_match = (direction == "BUY" and structure == "BULLISH_BOS") or (direction == "SELL" and structure == "BEARISH_BOS")
    breakdown.append(("Structure BOS", CONFLUENCE_WEIGHTS["structure_bos"] if bos_match else 0, CONFLUENCE_WEIGHTS["structure_bos"]))

    sweep_match = (direction == "BUY" and sweeps["bullish_sweep"]) or (direction == "SELL" and sweeps["bearish_sweep"])
    breakdown.append(("Liquidity Sweep", CONFLUENCE_WEIGHTS["liquidity_sweep"] if sweep_match else 0, CONFLUENCE_WEIGHTS["liquidity_sweep"]))

    fvg_match = (direction == "BUY" and fvg["bullish_fvg"]) or (direction == "SELL" and fvg["bearish_fvg"])
    breakdown.append(("Fair Value Gap", CONFLUENCE_WEIGHTS["fvg"] if fvg_match else 0, CONFLUENCE_WEIGHTS["fvg"]))

    breakdown.append(("Volume/ATR Activity", CONFLUENCE_WEIGHTS["volume_atr"] if vol_filter_ok else 0, CONFLUENCE_WEIGHTS["volume_atr"]))

    macd_match = (direction == "BUY" and macd_bias == "BULLISH") or (direction == "SELL" and macd_bias == "BEARISH")
    breakdown.append(("MACD Confirmation", CONFLUENCE_WEIGHTS["macd"] if macd_match else 0, CONFLUENCE_WEIGHTS["macd"]))

    pattern_match = (direction == "BUY" and candle_pattern in ("BULLISH_ENGULFING", "HAMMER")) or (direction == "SELL" and candle_pattern in ("BEARISH_ENGULFING", "SHOOTING_STAR"))
    breakdown.append(("Candlestick Pattern", CONFLUENCE_WEIGHTS["candlestick"] if pattern_match else (3 if candle_pattern == "DOJI" else 0), CONFLUENCE_WEIGHTS["candlestick"]))

    div_match = (direction == "BUY" and divergence.get("bullish")) or (direction == "SELL" and divergence.get("bearish"))
    breakdown.append(("RSI Divergence", CONFLUENCE_WEIGHTS["divergence"] if div_match else 0, CONFLUENCE_WEIGHTS["divergence"]))

    breakdown.append(("Support/Resistance", CONFLUENCE_WEIGHTS["sr_zone"] if sr_confluence else 0, CONFLUENCE_WEIGHTS["sr_zone"]))

    return sum(pts for _, pts, _ in breakdown), breakdown


def format_confluence_breakdown(score, breakdown):
    lines = [f"📊 **Confluence Score: {score}/100** ({score_label(score)})"]
    for label, earned, possible in breakdown:
        check = "✅" if earned == possible and possible > 0 else ("🟡" if earned > 0 else "❌")
        lines.append(f"  {check} {label}: `{earned}/{possible}`")
    return "\n".join(lines)


def simple_breakdown(is_bullish, close_price, near_zone, structure, entry_bullish, macro_bullish):
    """Light 4-factor breakdown the backtester uses for ema_cross / rsi_reversion factor stats."""
    sr_ok = bool(
        near_zone and (
            (is_bullish and near_zone["type"] in ("support", "mixed") and close_price >= near_zone["price"]) or
            ((not is_bullish) and near_zone["type"] in ("resistance", "mixed") and close_price <= near_zone["price"])
        )
    )
    want_bos = "BULLISH_BOS" if is_bullish else "BEARISH_BOS"
    return [
        ("EMA Trend Align", CONFLUENCE_WEIGHTS["ema_trend"] if (is_bullish == entry_bullish) else 0, CONFLUENCE_WEIGHTS["ema_trend"]),
        ("Macro Trend Align", CONFLUENCE_WEIGHTS["macro_trend"] if (is_bullish == macro_bullish) else 0, CONFLUENCE_WEIGHTS["macro_trend"]),
        ("Structure BOS", CONFLUENCE_WEIGHTS["structure_bos"] if structure == want_bos else 0, CONFLUENCE_WEIGHTS["structure_bos"]),
        ("Support/Resistance", CONFLUENCE_WEIGHTS["sr_zone"] if sr_ok else 0, CONFLUENCE_WEIGHTS["sr_zone"]),
    ]


# ---------------------------------------------------------------- targets

def calculate_targets(direction, close_price, atr_val, order_block, near_zone,
                      sl_mult=None, tp1_mult=None, tp2_mult=None):
    sl_mult = sl_mult if sl_mult is not None else ALERT_STATE["sl_atr_mult"]
    tp1_mult = tp1_mult if tp1_mult is not None else ALERT_STATE["tp1_atr_mult"]
    tp2_mult = tp2_mult if tp2_mult is not None else ALERT_STATE["tp2_atr_mult"]
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


# ---------------------------------------------------------------- reasoning text

def target_reasons(t):
    return [f"SL set by {t['sl_source']} ({t['sl_dist']} pts); TP1 by {t['tp1_source']}; RRR 1:{t['rrr']}"]


def build_smc_reasons(direction, a, score, breakdown, targets):
    is_buy = direction == "BUY"
    th = ALERT_STATE["rsi_buy_threshold"] if is_buy else ALERT_STATE["rsi_sell_threshold"]
    reasons = [f"RSI {a['rsi_val']} is {'oversold' if is_buy else 'overbought'} (trigger {'<=' if is_buy else '>='} {th})"]
    missing = []
    for label, earned, possible in breakdown:
        if label == "RSI Zone":
            continue
        if possible > 0 and earned == possible:
            text = _FACTOR_TEXT.get(label, label)
            if label == "Candlestick Pattern" and a.get("candle_pattern"):
                text = f"{a['candle_pattern'].replace('_', ' ').title()} candle on entry"
            reasons.append(text)
        elif earned == 0:
            missing.append(label)
    reasons.append(f"Confluence {score}/100 (minimum {ALERT_STATE['min_confluence_score']})")
    if missing:
        reasons.append("Not confirmed: " + ", ".join(missing))
    reasons += target_reasons(targets)
    return reasons


def format_reasons(reasons):
    if not reasons:
        return ""
    return "🧠 **Why this trade:**\n" + "\n".join(f"  • {r}" for r in reasons)