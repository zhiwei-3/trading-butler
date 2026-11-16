"""Confluence scoring for the SMC strategy. Pure."""

from utils.common import target_reasons

WEIGHTS = {
    "rsi_zone": 15,
    "ema_trend": 10,
    "macro_trend": 10,
    "structure_bos": 10,
    "liquidity_sweep": 10,
    "fvg": 10,
    "volume_atr": 10,
    "macd": 10,
    "candlestick": 5,
    "divergence": 5,
    "sr_zone": 5,
}

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


def score_label(score):
    if score >= 80: return "🔥 Very Strong"
    if score >= 65: return "✅ Strong"
    if score >= 50: return "⚠️ Moderate"
    return "❌ Weak"


def compute_confluence_score(direction, rsi_val, structure, sweeps, fvg, vol_filter_ok, macd_bias,
                             candle_pattern, divergence, sr_confluence, entry_bullish, macro_aligned):
    W = WEIGHTS
    is_buy = direction == "BUY"
    breakdown = []

    if (rsi_val <= 20) if is_buy else (rsi_val >= 80):
        rsi_pts = W["rsi_zone"]
    elif (rsi_val <= 25) if is_buy else (rsi_val >= 75):
        rsi_pts = 10
    else:
        rsi_pts = 6
    breakdown.append(("RSI Zone", rsi_pts, W["rsi_zone"]))

    ema_match = (is_buy and entry_bullish) or (not is_buy and not entry_bullish)
    breakdown.append(("EMA Trend Align", W["ema_trend"] if ema_match else 0, W["ema_trend"]))
    breakdown.append(("Macro Trend Align", W["macro_trend"] if macro_aligned else 0, W["macro_trend"]))

    bos_match = (is_buy and structure == "BULLISH_BOS") or (not is_buy and structure == "BEARISH_BOS")
    breakdown.append(("Structure BOS", W["structure_bos"] if bos_match else 0, W["structure_bos"]))

    sweep_match = (is_buy and sweeps["bullish_sweep"]) or (not is_buy and sweeps["bearish_sweep"])
    breakdown.append(("Liquidity Sweep", W["liquidity_sweep"] if sweep_match else 0, W["liquidity_sweep"]))

    fvg_match = (is_buy and fvg["bullish_fvg"]) or (not is_buy and fvg["bearish_fvg"])
    breakdown.append(("Fair Value Gap", W["fvg"] if fvg_match else 0, W["fvg"]))

    breakdown.append(("Volume/ATR Activity", W["volume_atr"] if vol_filter_ok else 0, W["volume_atr"]))

    macd_match = (is_buy and macd_bias == "BULLISH") or (not is_buy and macd_bias == "BEARISH")
    breakdown.append(("MACD Confirmation", W["macd"] if macd_match else 0, W["macd"]))

    pattern_match = ((is_buy and candle_pattern in ("BULLISH_ENGULFING", "HAMMER"))
                     or (not is_buy and candle_pattern in ("BEARISH_ENGULFING", "SHOOTING_STAR")))
    candle_pts = W["candlestick"] if pattern_match else (3 if candle_pattern == "DOJI" else 0)
    breakdown.append(("Candlestick Pattern", candle_pts, W["candlestick"]))

    div_match = (is_buy and divergence.get("bullish")) or (not is_buy and divergence.get("bearish"))
    breakdown.append(("RSI Divergence", W["divergence"] if div_match else 0, W["divergence"]))

    breakdown.append(("Support/Resistance", W["sr_zone"] if sr_confluence else 0, W["sr_zone"]))

    return sum(pts for _, pts, _ in breakdown), breakdown


def format_confluence_breakdown(score, breakdown):
    lines = [f"📊 **Confluence Score: {score}/100** ({score_label(score)})"]
    for label, earned, possible in breakdown:
        check = "✅" if earned == possible and possible > 0 else ("🟡" if earned > 0 else "❌")
        lines.append(f"  {check} {label}: `{earned}/{possible}`")
    return "\n".join(lines)


def build_reasons(direction, a, score, breakdown, targets, p):
    is_buy = direction == "BUY"
    th = p["rsi_buy"] if is_buy else p["rsi_sell"]
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
    reasons.append(f"Confluence {score}/100 (minimum {p['min_score']})")
    if missing:
        reasons.append("Not confirmed: " + ", ".join(missing))
    reasons += target_reasons(targets)
    return reasons