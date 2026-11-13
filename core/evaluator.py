# core/evaluator.py
"""
Market analysis, strategy dispatch and signal packaging (DB log + execution intent + chart).
Strategy RULES live in strategies/ (pure); everything with side effects lives here.
"""

import logging
import pandas_ta as ta

from config import ALERT_STATE, TIMEFRAME_PRESETS, TF_BY_NAME
from strategies import get_strategy, get_params, get_runtime, MarketData
from core.database import (
    log_signal_to_db, get_recent_trades_for_chart, set_signal_status, get_sl_cooldown_remaining_s,
)
from core.mt5_engine import fetch_candles, get_broker_utc_offset_seconds
from core.trade_engine import request_execution, trade_slot_blocker, calc_risk_lots
from utils.chart import generate_signal_chart
from utils.common import format_reasons
from utils.smc import (
    find_swing_points, detect_market_structure,
    detect_liquidity_sweeps, detect_fvg, detect_order_block, macro_fvg_snapshot,
)
from utils.indicators import (
    passes_volatility_filter, get_macd_bias, detect_candlestick_pattern,
    detect_divergence, find_sr_zones, nearest_sr_zone,
)


def _eval_setup_forming(a):
    """Global heads-up evaluator — runs for every strategy."""
    if not ALERT_STATE.get("setup_forming_enabled", True):
        return []

    rsi_val, close_price = a["rsi_val"], a["close_price"]
    buy_th = ALERT_STATE.get("rsi_buy_threshold", 30)
    sell_th = ALERT_STATE.get("rsi_sell_threshold", 70)
    margin = ALERT_STATE.get("watch_rsi_margin", 5)
    trend_bullish, macro_bullish = a["trend_bullish"], a["macro_bullish"]

    in_bullish_approach = (buy_th < rsi_val <= buy_th + margin)
    in_bearish_approach = (sell_th - margin <= rsi_val < sell_th)
    in_approach_zone = in_bullish_approach or in_bearish_approach

    watch_found = []
    if in_approach_zone:
        valid_bullish_watch = in_bullish_approach and (trend_bullish or macro_bullish)
        valid_bearish_watch = in_bearish_approach and (not trend_bullish or not macro_bullish)

        if (valid_bullish_watch or valid_bearish_watch) and ALERT_STATE.get("last_watch_signal") is None:
            ALERT_STATE["last_watch_signal"] = "FORMING"
            direction_icon = "🟢 BULLISH" if valid_bullish_watch else "🔴 BEARISH"
            watch_found.append(
                f"👀 **{direction_icon} SETUP FORMING (EARLY HEADS-UP)** 👀\n\n"
                f"• **Symbol:** `{a.get('symbol', 'XAUUSD')}` | **Price:** `${close_price}`\n"
                f"• **Current RSI:** `{rsi_val}` (Approaching Zone: `{buy_th}` / `{sell_th}`)\n"
                f"• **HTF Alignment:** Verified ✅\n"
                f"💡 *Monitor charts for imminent breakout or rejection.*"
            )
    else:
        ALERT_STATE["last_watch_signal"] = None

    return watch_found


def package_signal(a, direction, close_price, sl_price, tp1_price, tp2_price, base_msg, score=0, zone=None,
                   reasons=None, dynamic_sizing=False, dual=False):
    """Logs the signal, queues live execution (if armed and the slot is free), and renders the alert.
    Receives the kwargs of a strategy's utils.common.signal_spec(...)."""
    symbol = a.get("symbol", "XAUUSD")

    # ATR/risk-based sizing (only for strategies that opt in).
    lots_leg, sizing_text, sizing_blocker = None, "", None
    if dynamic_sizing and ALERT_STATE.get("dynamic_lots_enabled", True):
        sz = calc_risk_lots(symbol, abs(close_price - sl_price), legs=2 if dual else 1)
        if sz["ok"]:
            lots_leg = sz["lots"]
            sizing_text = (f"`{lots_leg}` lots per leg — risk `{sz['risk_money']:,.2f}` "
                           f"(`{ALERT_STATE.get('risk_percent')}%` of `{sz['balance']:,.2f}` balance, "
                           f"stop `{abs(close_price - sl_price):.2f}` pts)")
        else:
            sizing_blocker = sz["reason"]
            sizing_text = f"⚠️ unavailable — {sizing_blocker}"

    blocker = None
    if ALERT_STATE.get("auto_trade_enabled"):
        blocker = trade_slot_blocker(symbol) or sizing_blocker

    signal_id = log_signal_to_db(symbol, direction, close_price, sl_price, tp1_price, tp2_price, score,
                                 dual_entry=dual, near_zone=a.get("near_zone"),
                                 reasoning="\n".join(reasons) if reasons else None)

    if signal_id is None and ALERT_STATE.get("auto_trade_enabled") and not blocker:
        blocker = "the signal could not be logged to the database, so no untracked trade will be opened."

    if blocker:
        # Alert only — no order, and keep it out of the outcome tracker / stats.
        if signal_id:
            set_signal_status(signal_id, "SKIPPED")
    else:
        request_execution(signal_id, symbol, direction, close_price, sl_price, tp1_price, tp2_price,
                          score, dual_entry=dual, lots=lots_leg)

    lot_disp = lots_leg if lots_leg else ALERT_STATE.get("fixed_lot_size", 0.01)
    exec_note = ""
    pos_info = ""
    if blocker:
        exec_note = f"⏭️ *Alert only — no order placed:* {blocker}\n\n"
    else:
        if ALERT_STATE.get("auto_trade_enabled"):
            mode_lbl = "DRY-RUN" if ALERT_STATE.get("trade_dry_run", True) else "LIVE"
            if dual:
                exec_note = (
                    f"🤖 *Auto-trade {mode_lbl}* — 2 legs of `{lot_disp}` lots each: Leg 1 closes fully "
                    f"at TP1, Leg 2's SL trails to break-even the moment TP1 hits and runs for TP2.\n\n"
                )
            else:
                exec_note = (
                    f"🤖 *Auto-trade {mode_lbl}* — 1 leg of `{lot_disp}` lots, targeting **TP1 only** "
                    f"(this setup is single-leg).\n\n"
                )
        sizing_line = sizing_text if sizing_text else f"`{lot_disp}` lots per leg (fixed)"
        pos_info = (
            f"💼 **Position Plan:**\n"
            f"• **Sizing:** {sizing_line}\n"
            f"• **Legs:** {'2 — TP1 full close + TP2/BE runner 🎯🎯' if dual else '1 — TP1 only 🎯'}\n\n"
        )

    reasons_block = format_reasons(reasons)
    full_msg = base_msg + "\n\n" + (reasons_block + "\n\n" if reasons_block else "") + pos_info + exec_note

    try:
        trades_hist = get_recent_trades_for_chart(limit=30, symbol=symbol)
    except Exception:
        trades_hist = []

    chart_buf = None
    try:
        chart_buf = generate_signal_chart(
            a["df_entry"], symbol, direction, close_price, sl_price, tp1_price, tp2_price,
            fvg=zone or a.get("fvg"), near_zone=a.get("near_zone"), dual=dual,
            trades=trades_hist, exclude_id=signal_id)
    except Exception:
        logging.exception("Signal chart failed — sending alert without chart")
    return (full_msg, chart_buf)


def analyze_market(symbol):
    entry_tf, trend_tf, macro_tf = ALERT_STATE["entry_tf"], ALERT_STATE["trend_tf"], ALERT_STATE["macro_tf"]
    strat = get_strategy(ALERT_STATE.get("active_strategy"))

    df_entry = fetch_candles(symbol, entry_tf, strat.needs.entry_bars if strat else 200)
    df_trend = fetch_candles(symbol, trend_tf, 200)
    df_macro = fetch_candles(symbol, macro_tf, 200)
    if df_entry is None or df_trend is None or df_macro is None:
        return None

    df_entry['EMA_20'] = ta.ema(df_entry['close'], length=20)
    df_entry['EMA_50'] = ta.ema(df_entry['close'], length=50)
    df_trend['EMA_20'] = ta.ema(df_trend['close'], length=20)
    df_trend['EMA_50'] = ta.ema(df_trend['close'], length=50)
    df_macro['EMA_20'] = ta.ema(df_macro['close'], length=20)
    df_macro['EMA_50'] = ta.ema(df_macro['close'], length=50)
    df_entry['RSI'] = ta.rsi(df_entry['close'], length=14)
    df_entry['ATR'] = ta.atr(df_entry['high'], df_entry['low'], df_entry['close'], length=14)

    close_price = round(df_entry['close'].iloc[-1], 2)
    rsi_val = round(df_entry['RSI'].iloc[-1], 2)
    atr_val = round(df_entry['ATR'].iloc[-1], 2)

    swing_highs, swing_lows = find_swing_points(df_entry, ALERT_STATE["fractal_window"], ALERT_STATE["fractal_window"])
    sweeps = detect_liquidity_sweeps(df_entry, swing_highs, swing_lows)
    fvg = detect_fvg(df_entry)
    macro_fvg = macro_fvg_snapshot(df_macro, close_price, lookback=ALERT_STATE["sr_lookback"])
    order_block = detect_order_block(df_entry)
    sr_zones = find_sr_zones(df_macro, lookback=ALERT_STATE["sr_lookback"],
                             cluster_pct=ALERT_STATE["sr_cluster_pct"],
                             min_touches=ALERT_STATE["sr_min_touches"])
    near_zone = nearest_sr_zone(sr_zones, close_price, max_distance_pct=ALERT_STATE["sr_max_distance_pct"])
    vol_filter_ok, _ = passes_volatility_filter(df_entry,
                                                atr_multiplier=ALERT_STATE["atr_multiplier"],
                                                volume_multiplier=ALERT_STATE["volume_multiplier"])

    # Strategy connector: the strategy is pure, so WE fetch what it declared in `needs` and pass it in.
    ctx = None
    if strat is not None:
        try:
            extra = {name: fetch_candles(symbol, TF_BY_NAME[name], bars) for name, bars in strat.needs.extra}
            md = MarketData(symbol=symbol, entry=df_entry, trend=df_trend, macro=df_macro, extra=extra,
                            broker_offset_s=int(get_broker_utc_offset_seconds()))
            ctx = strat.build_live_context(md, get_params(strat.key))
        except Exception:
            logging.exception(f"Strategy '{strat.key}' build_live_context() failed — continuing without ctx.")

    return {
        "symbol": symbol,
        "df_entry": df_entry,
        "tf_label": TIMEFRAME_PRESETS[ALERT_STATE["timeframe_mode"]]["label"],
        "close_price": close_price,
        "rsi_val": rsi_val,
        "atr_val": atr_val,
        "entry_bullish": bool(df_entry['EMA_20'].iloc[-1] > df_entry['EMA_50'].iloc[-1]),
        "trend_bullish": bool(df_trend['EMA_20'].iloc[-1] > df_trend['EMA_50'].iloc[-1]),
        "macro_bullish": bool(df_macro['EMA_20'].iloc[-1] > df_macro['EMA_50'].iloc[-1]),
        "structure": detect_market_structure(df_entry),
        "sweeps": sweeps,
        "fvg": fvg,
        "macro_fvg": macro_fvg,
        "order_block": order_block,
        "vol_filter_ok": vol_filter_ok,
        "macd_bias": get_macd_bias(df_entry),
        "candle_pattern": detect_candlestick_pattern(df_entry),
        "divergence": detect_divergence(df_entry),
        "near_zone": near_zone,
        "ctx": ctx,
    }


def evaluate_signals(a):
    """Runs the active strategy's pure rules, then packages each resulting signal spec."""
    strat = get_strategy(ALERT_STATE.get("active_strategy"))
    if strat is None:
        return [], []          # nothing selected/installed -> no signals, no heads-up pings

    p, mem = get_params(strat.key), get_runtime(strat.key)

    watches = []
    if ALERT_STATE.get("setup_forming_enabled", True):
        try:
            watches = strat.watch(a, p, mem) or []
        except Exception:
            logging.exception(f"Strategy '{strat.key}' watch() crashed — no heads-up this tick.")

    # Post-stop-loss cooldown: heads-up pings still work, new trade signals don't.
    cooldown_s = get_sl_cooldown_remaining_s()
    if cooldown_s > 0:
        logging.info(f"⏸️ SL cooldown active — {cooldown_s / 60:.1f} min remaining, signals suppressed.")
        return [], watches

    if ALERT_STATE.get("require_volume_atr_filter", False) and not a.get("vol_filter_ok", True):
        return [], watches

    try:
        specs = strat.evaluate(a, p, mem) or []
    except Exception:
        logging.exception(f"Strategy '{strat.key}' evaluate() crashed — no signals this tick.")
        return [], watches
    return [package_signal(a, **spec) for spec in specs], watches