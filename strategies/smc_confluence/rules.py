"""PURE rules: one scan shared by live and backtest."""

from .._lib import atr_targets
from .scoring import compute_confluence_score


def scan(a, p, structure_required, last):
    """
    a: analysis dict (live `a` or backtest `c["a"]`).  last: dedup memory ("BUY"|"SELL"|None).
    Returns (found, new_last). found = {"direction","score","breakdown","targets"} or None.
    """
    rsi, close = a["rsi_val"], a["close_price"]
    structure, near = a["structure"], a["near_zone"]
    oversold, overbought = rsi <= p["rsi_buy"], rsi >= p["rsi_sell"]

    direction = None
    if (oversold and (a["trend_bullish"] or a["macro_bullish"])
            and (not structure_required or structure == "BULLISH_BOS")):
        direction = "BUY"
        sr = bool(near and near["type"] in ("support", "mixed") and close >= near["price"])
        macro_aligned = a["macro_bullish"]
    elif (overbought and (not a["trend_bullish"] or not a["macro_bullish"])
            and (not structure_required or structure == "BEARISH_BOS")):
        direction = "SELL"
        sr = bool(near and near["type"] in ("resistance", "mixed") and close <= near["price"])
        macro_aligned = not a["macro_bullish"]

    if direction and last != direction:
        score, breakdown = compute_confluence_score(
            direction, rsi, structure, a["sweeps"], a["fvg"], a["vol_filter_ok"], a["macd_bias"],
            a["candle_pattern"], a["divergence"], sr, a["entry_bullish"], macro_aligned)
        if score >= p["min_score"]:
            targets = atr_targets(direction, a, p)
            if targets["rrr"] >= p["min_rrr"]:
                return {"direction": direction, "score": score, "breakdown": breakdown, "targets": targets}, direction

    # Dedup clears only once RSI has left the extreme zones (mirrors live and backtest alike).
    return None, (None if not (oversold or overbought) else last)