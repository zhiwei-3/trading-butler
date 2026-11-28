# strategies/_lib.py
"""Reusable Param groups + helpers. Strategies import what they use: `from .._lib import risk_params, ...`
Nothing here is mandatory — a strategy with its own risk model simply doesn't import it."""

from .base import Param
from utils.common import calculate_targets
from utils.parsers import num, boolean


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


def sizing_params(dynamic=False, risk_pct=0.5, max_lot=1.0, legacy_dynamic=False):
    """Position-sizing group. dynamic=False -> fixed lots (the global fixed_lot_size).
    legacy_dynamic=True migrates the old global `dynamic_lots_enabled` (only for strategies that used it)."""
    return (
        Param("dynamic_lots", dynamic, boolean, ("dyn_lots",),
              "size each trade by risk % of balance (off = fixed lots)",
              legacy="dynamic_lots_enabled" if legacy_dynamic else ""),
        Param("risk_pct", risk_pct, num(0, 10, lo_open=True), ("risk",),
              "% of balance risked per trade (shared across legs)", legacy="risk_percent"),
        Param("max_lot", max_lot, num(0, 100, lo_open=True), (),
              "cap on lots per leg when sizing dynamically", legacy="max_lot_size"),
    )


def sizing_spec(p):
    """What a strategy hands the evaluator: the risk to size by, or None for fixed lots."""
    return {"risk_pct": p["risk_pct"], "max_lot": p["max_lot"]} if p["dynamic_lots"] else None


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


def volume_gate_params(on=False):
    """Hard gate, enforced generically by the evaluator and backtester: skip the strategy's signals unless
    ATR and tick volume are above their recent averages (thresholds: the shared atr_/volume_multiplier)."""
    return (
        Param("require_volume", on, boolean, ("volume", "vol_gate"),
              "hard gate: ATR and tick volume must be above their recent averages",
              sweep=(False, True), legacy="require_volume_atr_filter"),
    )


def structure_gate_params(on=False):
    """Hard gate honoured by the strategy's own rules (direction-dependent). Include only if the rules read it."""
    return (
        Param("require_structure", on, boolean, ("structure", "bos_gate"),
              "hard gate: needs a fresh break of structure in the trade direction",
              sweep=(False, True), legacy="require_structure_break"),
    )


# ---------------------------------------------------------------- targets
def atr_targets(direction, a, p):
    """ATR/OB/S&R targets from an analysis dict (live `a` or backtest `c["a"]`). Missing features degrade gracefully."""
    return calculate_targets(direction, a["close_price"], a["atr_val"], a.get("order_block") or {},
                             a.get("near_zone"), p["sl_mult"], p["tp1_mult"], p["tp2_mult"])


def atr_targets_bt(direction, c):
    return atr_targets(direction, c["a"], c["p"])


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