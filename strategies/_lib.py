# strategies/_lib.py
"""Reusable Param groups + helpers. Strategies import what they use: `from .._lib import risk_params, ...`
Nothing here is mandatory — a strategy with its own risk model simply doesn't import it."""

from .base import Param
from utils.common import calculate_targets
from utils.parsers import num


def risk_params(min_rrr=1.5, sl=1.2, tp1=2.0, tp2=3.5):
    """ATR-multiple risk model (used with atr_targets())."""
    return (
        Param("min_rrr", min_rrr, num(0, 20, lo_open=True), ("rrr",),
              "minimum TP1:SL reward-to-risk to fire",
              sweep=(1.0, 1.2, 1.3, 1.5, 1.8, 2.0, 2.5, 3.0), legacy="min_rrr"),
        Param("sl_mult", sl, num(0, 20, lo_open=True), ("sl", "sl_atr"),
              "SL distance (ATR multiple)",
              sweep=(1.0, 1.2, 1.5, 1.7, 2.0, 2.2, 2.5, 3.0), legacy="sl_atr_mult"),
        Param("tp1_mult", tp1, num(0, 20, lo_open=True), ("tp1", "tp1_atr"),
              "TP1 distance (ATR multiple)", legacy="tp1_atr_mult"),
        Param("tp2_mult", tp2, num(0, 20, lo_open=True), ("tp2", "tp2_atr"),
              "TP2 distance (ATR multiple)", legacy="tp2_atr_mult"),
    )


def score_params(min_score=35, dual_score=50):
    """Confluence-score gate + the score that unlocks the 2-leg entry."""
    return (
        Param("min_score", min_score, num(0, 100, integer=True), ("score", "min_confluence_score"),
              "minimum confluence score (0-100)",
              sweep=(15, 20, 25, 30, 35, 40, 45, 50), legacy="min_confluence_score"),
        Param("dual_score", dual_score, num(0, 100, integer=True), ("dual_threshold",),
              "score at/above which 2 legs open", legacy="dual_entry_score_threshold"),
    )


def rsi_params(buy=30.0, sell=70.0, margin=5.0):
    return (
        Param("rsi_buy", buy, num(1, 99), ("buy_rsi",), "RSI at/below which BUY setups arm",
              sweep=(25, 30, 35, 40), legacy="rsi_buy_threshold"),
        Param("rsi_sell", sell, num(1, 99), ("sell_rsi",), "RSI at/above which SELL setups arm",
              sweep=(60, 65, 70), legacy="rsi_sell_threshold"),
        Param("watch_margin", margin, num(0, 50), (), "RSI distance from the trigger that fires a heads-up",
              legacy="watch_rsi_margin"),
    )


def rsi_valid(p):
    return p["rsi_buy"] < p["rsi_sell"]


# ---------------------------------------------------------------- targets
def atr_targets(direction, a, p):
    """Live: ATR/OB/S&R targets from the analysis dict `a` and the strategy's params."""
    return calculate_targets(direction, a["close_price"], a["atr_val"], a["order_block"], a["near_zone"],
                             p["sl_mult"], p["tp1_mult"], p["tp2_mult"])


def atr_targets_bt(direction, c):
    """Backtest: same thing from the backtester context `c`."""
    p = c["p"]
    return calculate_targets(direction, c["close_price"], c["atr_val"], c["full"].get("order_block", {}),
                             c["near_zone"], p["sl_mult"], p["tp1_mult"], p["tp2_mult"])


# ---------------------------------------------------------------- heads-up
def rsi_watch(a, p, mem):
    """RSI-approach 'setup forming' ping (moved out of the evaluator). Debounce lives in `mem`."""
    rsi_val, close_price = a["rsi_val"], a["close_price"]
    buy_th, sell_th, margin = p["rsi_buy"], p["rsi_sell"], p["watch_margin"]
    trend_bullish, macro_bullish = a["trend_bullish"], a["macro_bullish"]

    bull = buy_th < rsi_val <= buy_th + margin
    bear = sell_th - margin <= rsi_val < sell_th
    if not (bull or bear):
        mem["watch_state"] = None
        return []

    valid_bull = bull and (trend_bullish or macro_bullish)
    valid_bear = bear and (not trend_bullish or not macro_bullish)
    if not (valid_bull or valid_bear) or mem.get("watch_state") is not None:
        return []

    mem["watch_state"] = "FORMING"
    icon = "🟢 BULLISH" if valid_bull else "🔴 BEARISH"
    return [
        f"👀 **{icon} SETUP FORMING (EARLY HEADS-UP)** 👀\n\n"
        f"• **Symbol:** `{a.get('symbol', 'XAUUSD')}` | **Price:** `${close_price}`\n"
        f"• **Current RSI:** `{rsi_val}` (Approaching Zone: `{buy_th}` / `{sell_th}`)\n"
        f"• **HTF Alignment:** Verified ✅\n"
        f"💡 *Monitor charts for imminent breakout or rejection.*"
    ]