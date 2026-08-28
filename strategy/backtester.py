import io
import numpy as np
import pandas as pd
import pandas_ta as ta
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta

from config import ALERT_STATE, TIMEFRAME_PRESETS
from mt5_engine import MT5_LOCK
from strategy.smc import (
    find_swing_points, detect_market_structure,
    detect_liquidity_sweeps, detect_fvg, detect_order_block
)
from strategy.indicators import (
    detect_candlestick_pattern, detect_divergence, find_sr_zones, nearest_sr_zone
)
from strategy.evaluator import compute_confluence_score, calculate_targets

MIN_WARMUP_BARS = 210
MAX_BARS_FORWARD = 200
ENTRY_WINDOW = 200
HTF_WINDOW = 200
ATR_AVG_PERIOD = 20
VOLUME_AVG_PERIOD = 20
MIN_INDICATOR_BARS = 80  # comfortably above the EMA_50 requirement, with margin

_TF_MINUTES = {
    mt5.TIMEFRAME_M1: 1, mt5.TIMEFRAME_M5: 5, mt5.TIMEFRAME_M15: 15, mt5.TIMEFRAME_M30: 30,
    mt5.TIMEFRAME_H1: 60, mt5.TIMEFRAME_H4: 240, mt5.TIMEFRAME_D1: 1440,
}


def _min_warmup_days_for(timeframe, min_bars=MIN_INDICATOR_BARS):
    """Calendar days needed to guarantee `min_bars` of a given timeframe. A flat warmup
    buffer works fine for M5/M15/H1 but silently starves D1 (swing mode's macro
    timeframe) of enough bars to compute EMA_50 at all — 30 calendar days of D1 is only
    ~21-22 trading bars. Pad by 1.5x for D1+ to account for weekends/holidays."""
    minutes = _TF_MINUTES.get(timeframe, 60)
    calendar_days = (minutes * min_bars) / (60 * 24)
    if minutes >= 1440:
        calendar_days *= 1.5
    return calendar_days


def _fetch_history(symbol, timeframe, start, end):
    """Pulls a full historical range from MT5. Returns None if MT5 has no data for the range."""
    with MT5_LOCK:
        rates = mt5.copy_rates_range(symbol, timeframe, start, end)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True).astype('datetime64[ns, UTC]')
    return df


def _precompute_indicators(df):
    """Adds EMA/RSI/ATR columns. These are causal — each row only depends on prior rows.
    pandas_ta returns None (not a NaN-filled Series) when there aren't enough bars for the
    requested length — e.g. EMA_50 on a short D1 history for swing mode. Assigning None
    directly creates an object-dtype column, which later blows up any np.isnan() check
    with a TypeError instead of behaving like a normal missing value. pd.to_numeric with
    errors='coerce' guarantees a proper float64 NaN column either way."""
    df = df.copy()
    df['EMA_20'] = pd.to_numeric(ta.ema(df['close'], length=20), errors='coerce')
    df['EMA_50'] = pd.to_numeric(ta.ema(df['close'], length=50), errors='coerce')
    df['RSI'] = pd.to_numeric(ta.rsi(df['close'], length=14), errors='coerce')
    df['ATR'] = pd.to_numeric(ta.atr(df['high'], df['low'], df['close'], length=14), errors='coerce')
    return df


def _precompute_entry_extras(df):
    """One-time, whole-series versions of the ATR-average/volume-filter and MACD-bias
    calculations that the live evaluator normally recomputes on a fresh 200-bar window
    every single call. Doing it once here instead of inside the bar-by-bar loop is the
    single biggest speedup available: ta.atr/ta.macd have real per-call overhead that
    adds up fast across thousands of bars. Both are RMA/EMA-based, so they converge to
    the same values as a windowed recompute once enough warmup bars have passed — which
    the MIN_WARMUP_BARS cutoff below guarantees before any bar is actually scored."""
    df = df.copy()
    df['ATR_AVG'] = df['ATR'].rolling(ATR_AVG_PERIOD).mean()
    df['VOL_AVG'] = df['tick_volume'].rolling(VOLUME_AVG_PERIOD).mean() if 'tick_volume' in df.columns else np.nan

    macd_df = ta.macd(df['close'], fast=12, slow=26, signal=9)
    if macd_df is not None and not macd_df.empty:
        macd_col = next((c for c in macd_df.columns if c.startswith("MACD_")), None)
        signal_col = next((c for c in macd_df.columns if c.startswith("MACDs_")), None)
        if macd_col and signal_col:
            df['MACD_BIAS'] = np.where(macd_df[macd_col] > macd_df[signal_col], "BULLISH", "BEARISH")
        else:
            df['MACD_BIAS'] = None
    else:
        df['MACD_BIAS'] = None
    return df


def _vol_filter_ok(row, atr_multiplier, volume_multiplier):
    atr_ok = True
    if pd.notna(row['ATR_AVG']) and pd.notna(row['ATR']):
        atr_ok = bool(row['ATR'] >= row['ATR_AVG'] * atr_multiplier)
    vol_ok = True
    if 'tick_volume' in row.index and pd.notna(row.get('VOL_AVG')) and pd.notna(row['tick_volume']):
        vol_ok = bool(row['tick_volume'] >= row['VOL_AVG'] * volume_multiplier)
    return atr_ok and vol_ok


def _advance_cursor(times_arr, cursor, ts):
    """Forward-only cursor: moves to the last bar at-or-before ts. The main loop sweeps
    forward in time monotonically, so this is O(1) amortized instead of a fresh binary
    search + DataFrame slice/copy on every single entry-tf bar."""
    n = len(times_arr)
    while cursor + 1 < n and times_arr[cursor + 1] <= ts:
        cursor += 1
    return cursor


def _full_analysis(entry_slice, state):
    """The expensive, structurally-windowed SMC/pattern detectors. Only called for bars
    that already passed the cheap RSI+trend+macro gate below, since those are the only
    bars where the result could possibly change the outcome."""
    swing_highs, swing_lows = find_swing_points(entry_slice, state["fractal_window"], state["fractal_window"])
    return {
        "sweeps": detect_liquidity_sweeps(entry_slice, swing_highs, swing_lows),
        "fvg": detect_fvg(entry_slice),
        "order_block": detect_order_block(entry_slice),
        "structure": detect_market_structure(entry_slice),
        "macd_bias": entry_slice['MACD_BIAS'].iloc[-1] if 'MACD_BIAS' in entry_slice.columns else None,
        "candle_pattern": detect_candlestick_pattern(entry_slice),
        "divergence": detect_divergence(entry_slice),
    }


def _decide_trade(close_price, rsi_val, atr_val, entry_bullish, trend_bullish, macro_bullish,
                   full, state, min_confluence_score, min_rrr, strategy_name="smc_confluence"):
    """Mirrors strategy.evaluator.evaluate_signals' BUY/SELL branch, but writes debounce
    state locally instead of mutating the global ALERT_STATE, and returns a trade dict
    instead of dispatching a Telegram message."""

    if strategy_name == "htf_fvg_sweep":
        fvg, sweeps, ob = full["fvg"], full["sweeps"], full["order_block"]
        macro_fvg = full.get("macro_fvg", {})

        htf_bull_tap = macro_fvg.get("bullish_fvg", False)
        htf_bear_tap = macro_fvg.get("bearish_fvg", False)

        bullish_setup = (
            htf_bull_tap and sweeps.get("bullish_sweep", False) and 
            structure == "BULLISH_BOS" and fvg.get("bullish_fvg", False)
        )
        bearish_setup = (
            htf_bear_tap and sweeps.get("bearish_sweep", False) and 
            structure == "BEARISH_BOS" and fvg.get("bearish_fvg", False)
        )

        if bullish_setup:
            targets = calculate_targets("BUY", close_price, atr_val, ob, near_zone)
            if targets["rrr"] >= min_rrr:
                return {"direction": "BUY", "entry": close_price, "score": 90, "breakdown": [], **targets}

        elif bearish_setup:
            targets = calculate_targets("SELL", close_price, atr_val, ob, near_zone)
            if targets["rrr"] >= min_rrr:
                return {"direction": "SELL", "entry": close_price, "score": 90, "breakdown": [], **targets}

        return None

    buy_th, sell_th = state["rsi_buy_threshold"], state["rsi_sell_threshold"]
    structure = full["structure"]
    near_zone = nearest_sr_zone(full["sr_zones"], close_price)

    buy_structure_ok = (not state["require_structure_break"]) or (structure == "BULLISH_BOS")
    sell_structure_ok = (not state["require_structure_break"]) or (structure == "BEARISH_BOS")

    if rsi_val <= buy_th and (trend_bullish or macro_bullish) and buy_structure_ok:
        sr_confluence = bool(near_zone and near_zone["type"] in ("support", "mixed") and close_price >= near_zone["price"])
        score, breakdown = compute_confluence_score(
            "BUY", rsi_val, structure, full["sweeps"], full["fvg"], full["vol_filter_ok"],
            full["macd_bias"], full["candle_pattern"], full["divergence"], sr_confluence,
            entry_bullish, macro_bullish
        )
        if score >= min_confluence_score and state["last_signal"] != "BUY":
            targets = calculate_targets("BUY", close_price, atr_val, full["order_block"], near_zone)
            if targets["rrr"] >= min_rrr:
                state["last_signal"] = "BUY"
                return {"direction": "BUY", "entry": close_price, "score": score, "breakdown": breakdown, **targets}
        return None

    if rsi_val >= sell_th and ((not trend_bullish) or (not macro_bullish)) and sell_structure_ok:
        sr_confluence = bool(near_zone and near_zone["type"] in ("resistance", "mixed") and close_price <= near_zone["price"])
        score, breakdown = compute_confluence_score(
            "SELL", rsi_val, structure, full["sweeps"], full["fvg"], full["vol_filter_ok"],
            full["macd_bias"], full["candle_pattern"], full["divergence"], sr_confluence,
            entry_bullish, not macro_bullish
        )
        if score >= min_confluence_score and state["last_signal"] != "SELL":
            targets = calculate_targets("SELL", close_price, atr_val, full["order_block"], near_zone)
            if targets["rrr"] >= min_rrr:
                state["last_signal"] = "SELL"
                return {"direction": "SELL", "entry": close_price, "score": score, "breakdown": breakdown, **targets}
        return None

    state["last_signal"] = None
    return None


def _simulate_trade(df_entry, entry_idx, trade, spread_price=0.0, max_bars_forward=MAX_BARS_FORWARD):
    """Walks forward bar-by-bar from the entry index, resolving SL/TP1/TP2 against each
    subsequent bar's high/low. Applies a synthetic spread cost and moves SL to breakeven
    once TP1 is tagged. Once TP1 has been banked, a later retrace back to breakeven is
    scored as a TP1 win (the profit was already locked in), never as a stop-loss."""
    direction = trade["direction"]
    sl, tp1, tp2 = trade["sl_price"], trade["tp1_price"], trade["tp2_price"]
    end_idx = min(entry_idx + max_bars_forward, len(df_entry) - 1)
    hit_tp1 = False

    highs = df_entry['high'].values
    lows = df_entry['low'].values
    times = df_entry['time'].values

    for i in range(entry_idx + 1, end_idx + 1):
        high, low = highs[i], lows[i]

        if direction == "BUY":
            sim_low, sim_high = low, high + spread_price
            if not hit_tp1:
                if sim_low <= sl:
                    return "HIT_SL", times[i]
                if sim_high >= tp1:
                    hit_tp1 = True
                    sl = trade["entry"]
                    if sim_high >= tp2:
                        return "HIT_TP2", times[i]
                    continue
            else:
                if sim_low <= sl:
                    return "HIT_TP1", times[i]  # already banked TP1 — this is a win, not a stop-out
                if sim_high >= tp2:
                    return "HIT_TP2", times[i]
        else:
            sim_low, sim_high = low - spread_price, high
            if not hit_tp1:
                if sim_high >= sl:
                    return "HIT_SL", times[i]
                if sim_low <= tp1:
                    hit_tp1 = True
                    sl = trade["entry"]
                    if sim_low <= tp2:
                        return "HIT_TP2", times[i]
                    continue
            else:
                if sim_high >= sl:
                    return "HIT_TP1", times[i]  # already banked TP1 — this is a win, not a stop-out
                if sim_low <= tp2:
                    return "HIT_TP2", times[i]

    return ("HIT_TP1" if hit_tp1 else "OPEN"), times[end_idx]


def run_backtest(symbol, days=30, timeframe_mode=None, strategy_name=None, 
                 min_confluence_score=None, min_rrr=None, spread_pips=2.0, progress_callback=None):
    """
    Replays the live strategy bar-by-bar over historical MT5 data with isolated debounce
    state (never touches the live ALERT_STATE). Expensive SMC/pattern detection is only
    run on bars that pass a cheap RSI+trend+macro gate first, and HTF (trend/macro)
    lookups use forward-only cursors with S/R-zone caching instead of rebuilding slices
    every entry-tf bar — both are what make this fast enough to run interactively instead
    of taking minutes.

    progress_callback, if given, is called with an int 0-100 roughly every 10% of bars.
    """
    active_strat = strategy_name or ALERT_STATE.get("active_strategy", "smc_confluence")
    mode = timeframe_mode or ALERT_STATE["timeframe_mode"]
    if mode not in TIMEFRAME_PRESETS:
        return {"error": f"Unknown timeframe mode '{mode}'"}
    preset = TIMEFRAME_PRESETS[mode]

    state = {
        "rsi_buy_threshold": ALERT_STATE["rsi_buy_threshold"],
        "rsi_sell_threshold": ALERT_STATE["rsi_sell_threshold"],
        "require_structure_break": ALERT_STATE["require_structure_break"],
        "require_volume_atr_filter": ALERT_STATE["require_volume_atr_filter"],
        "fractal_window": ALERT_STATE["fractal_window"],
        "last_signal": None,
    }
    atr_multiplier = ALERT_STATE["atr_multiplier"]
    volume_multiplier = ALERT_STATE["volume_multiplier"]
    min_confluence_score = min_confluence_score if min_confluence_score is not None else ALERT_STATE["min_confluence_score"]
    min_rrr = min_rrr if min_rrr is not None else ALERT_STATE["min_rrr"]
    spread_price = spread_pips / 10.0  # XAUUSD: 1 pip = $0.10, matching the /calc convention

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    warmup_days = max(
        10, days // 4,
        _min_warmup_days_for(preset["entry"]),
        _min_warmup_days_for(preset["trend"]),
        _min_warmup_days_for(preset["macro"]),
    )
    warmup_start = start - timedelta(days=warmup_days)

    df_entry_raw = _fetch_history(symbol, preset["entry"], warmup_start, end)
    df_trend_raw = _fetch_history(symbol, preset["trend"], warmup_start, end)
    df_macro_raw = _fetch_history(symbol, preset["macro"], warmup_start, end)
    if df_entry_raw is None or df_trend_raw is None or df_macro_raw is None:
        return {"error": "Insufficient MT5 historical data returned for the requested range/timeframe. Try a shorter range or check your broker's history depth."}

    df_entry = _precompute_entry_extras(_precompute_indicators(df_entry_raw))
    df_trend = _precompute_indicators(df_trend_raw)
    df_macro = _precompute_indicators(df_macro_raw)

    if df_trend['EMA_50'].notna().sum() == 0 or df_macro['EMA_50'].notna().sum() == 0:
        return {"error": "Not enough historical bars on the trend/macro timeframe to compute a 50-period EMA for this mode. Your broker may not have enough cached history — try scrolling further back on that timeframe's chart in the MT5 terminal first, or use a shorter timeframe mode."}

    first_valid_idx = df_entry['ATR_AVG'].first_valid_index() or 0
    test_start_idx = max(MIN_WARMUP_BARS, first_valid_idx, int(df_entry['time'].searchsorted(start, side='left')))
    if test_start_idx >= len(df_entry) - 1:
        return {"error": "Not enough historical bars after warmup to run a backtest over this range."}

    # Hot-loop numpy arrays — avoids repeated pandas .iloc overhead across thousands of bars
    close_arr = df_entry['close'].values
    time_arr = df_entry['time'].values
    rsi_arr = df_entry['RSI'].values
    atr_arr = df_entry['ATR'].values
    ema20_arr = df_entry['EMA_20'].values
    ema50_arr = df_entry['EMA_50'].values

    trend_time_arr = df_trend['time'].values
    trend_ema20_arr = df_trend['EMA_20'].values
    trend_ema50_arr = df_trend['EMA_50'].values

    macro_time_arr = df_macro['time'].values
    macro_ema20_arr = df_macro['EMA_20'].values
    macro_ema50_arr = df_macro['EMA_50'].values

    trend_cursor = max(0, int(np.searchsorted(trend_time_arr, time_arr[test_start_idx], side='right')) - 1)
    macro_cursor = max(0, int(np.searchsorted(macro_time_arr, time_arr[test_start_idx], side='right')) - 1)

    cached_sr_macro_cursor = -1
    cached_sr_zones = []

    trades = []
    factor_stats = {}
    equity = [0.0]

    total_iters = max(1, len(df_entry) - test_start_idx)
    last_reported_pct = -1

    for pos, i in enumerate(range(test_start_idx, len(df_entry))):
        if progress_callback:
            pct = int(pos / total_iters * 100)
            if pct >= last_reported_pct + 10:
                last_reported_pct = pct
                try:
                    progress_callback(pct)
                except Exception:
                    pass

        ts = time_arr[i]
        rsi_val, close_price, atr_val = rsi_arr[i], close_arr[i], atr_arr[i]
        if np.isnan(rsi_val) or np.isnan(atr_val) or atr_val <= 0:
            continue

        trend_cursor = _advance_cursor(trend_time_arr, trend_cursor, ts)
        macro_cursor = _advance_cursor(macro_time_arr, macro_cursor, ts)
        if trend_cursor < 1 or macro_cursor < 1:
            continue
        if (np.isnan(trend_ema20_arr[trend_cursor]) or np.isnan(trend_ema50_arr[trend_cursor])
                or np.isnan(macro_ema20_arr[macro_cursor]) or np.isnan(macro_ema50_arr[macro_cursor])):
            continue

        entry_bullish = ema20_arr[i] > ema50_arr[i]  # computed for parity with live analyze_market; not gated on here
        trend_bullish = bool(trend_ema20_arr[trend_cursor] > trend_ema50_arr[trend_cursor])
        macro_bullish = bool(macro_ema20_arr[macro_cursor] > macro_ema50_arr[macro_cursor])

        buy_th, sell_th = state["rsi_buy_threshold"], state["rsi_sell_threshold"]
        buy_gate = rsi_val <= buy_th and (trend_bullish or macro_bullish)
        sell_gate = rsi_val >= sell_th and ((not trend_bullish) or (not macro_bullish))

        # Cheap gate first: most bars sit outside both RSI zones and get thrown away
        # immediately in live logic anyway, so skip all expensive SMC detection for them.
        if not (buy_gate or sell_gate):
            state["last_signal"] = None
            continue

        vol_filter_ok = True
        if state["require_volume_atr_filter"]:
            vol_filter_ok = _vol_filter_ok(df_entry.iloc[i], atr_multiplier, volume_multiplier)
            if not vol_filter_ok:
                state["last_signal"] = None
                continue

        entry_slice = df_entry.iloc[max(0, i - (ENTRY_WINDOW - 1)):i + 1]

        # S/R zones only depend on the macro timeframe, which updates far less often than
        # the entry timeframe (e.g. 12 M5 bars per H1 candle on scalp mode) — cache and
        # only recompute when the underlying macro bar actually changes.
        if macro_cursor != cached_sr_macro_cursor:
            macro_slice = df_macro.iloc[max(0, macro_cursor - (HTF_WINDOW - 1)):macro_cursor + 1].reset_index(drop=True)
            cached_sr_zones = find_sr_zones(macro_slice)
            cached_sr_macro_cursor = macro_cursor

        full = _full_analysis(entry_slice, state)
        full["sr_zones"] = cached_sr_zones
        full["vol_filter_ok"] = vol_filter_ok

        trade = _decide_trade(
            round(float(close_price), 2), round(float(rsi_val), 2), round(float(atr_val), 2),
            entry_bullish, trend_bullish, macro_bullish, full, state, min_confluence_score, min_rrr,
            strategy_name=active_strat
        )
        if trade is None:
            continue

        outcome, close_time = _simulate_trade(df_entry, i, trade, spread_price=spread_price)

        if outcome == "HIT_SL":
            r_multiple = -1.0
        elif outcome == "HIT_TP1":
            r_multiple = round(trade["tp1_dist"] / trade["sl_dist"], 2) if trade["sl_dist"] > 0 else 0.0
        elif outcome == "HIT_TP2":
            r_multiple = round(trade["tp2_dist"] / trade["sl_dist"], 2) if trade["sl_dist"] > 0 else 0.0
        else:
            r_multiple = 0.0

        trades.append({
            "time": ts, "direction": trade["direction"], "entry": trade["entry"],
            "sl": trade["sl_price"], "tp1": trade["tp1_price"], "tp2": trade["tp2_price"],
            "score": trade["score"], "outcome": outcome, "r_multiple": r_multiple,
            "closed_at": close_time,
        })
        equity.append(round(equity[-1] + r_multiple, 2))

        won = outcome in ("HIT_TP1", "HIT_TP2")
        if outcome in ("HIT_TP1", "HIT_TP2", "HIT_SL"):
            for label, earned, possible in trade["breakdown"]:
                if possible <= 0:
                    continue
                fs = factor_stats.setdefault(label, {"wins": 0, "present": 0})
                if earned == possible:
                    fs["present"] += 1
                    if won:
                        fs["wins"] += 1

    if progress_callback:
        try:
            progress_callback(100)
        except Exception:
            pass

    total = len(trades)
    wins = sum(1 for t in trades if t["outcome"] in ("HIT_TP1", "HIT_TP2"))
    losses = sum(1 for t in trades if t["outcome"] == "HIT_SL")
    still_open = sum(1 for t in trades if t["outcome"] == "OPEN")
    closed = wins + losses
    win_rate = round((wins / closed * 100), 1) if closed > 0 else 0.0
    closed_r = [t["r_multiple"] for t in trades if t["outcome"] != "OPEN"]
    avg_r = round(sum(closed_r) / len(closed_r), 2) if closed_r else 0.0
    net_r = round(sum(closed_r), 2)

    peak, max_dd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, peak - e)

    factor_summary = [
        (label, round(fs["wins"] / fs["present"] * 100, 1), fs["present"])
        for label, fs in factor_stats.items() if fs["present"] > 0
    ]
    factor_summary.sort(key=lambda x: x[1], reverse=True)

    return {
        "symbol": symbol, "mode": mode, "days": days,
        "min_confluence_score": min_confluence_score, "min_rrr": min_rrr,
        "total_trades": total, "wins": wins, "losses": losses, "open": still_open,
        "win_rate": win_rate, "avg_r": avg_r, "net_r": net_r, "max_drawdown_r": round(max_dd, 2),
        "equity_curve": equity, "trades": trades, "factor_summary": factor_summary,
    }


def generate_equity_chart(equity_curve, title="Backtest Equity Curve"):
    """Renders the cumulative R-multiple equity curve into an in-memory PNG buffer.
    Returns None on failure so callers can gracefully fall back to a text-only reply."""
    if not equity_curve or len(equity_curve) < 2:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None

    fig, ax = plt.subplots(figsize=(8, 4.5), facecolor="#1b1b1b")
    ax.set_facecolor("#1b1b1b")
    ax.plot(equity_curve, color="#2962ff", linewidth=1.8)
    ax.axhline(0, color="#666666", linewidth=0.8, linestyle="--")
    ax.set_title(title, color="white", fontsize=11)
    ax.set_xlabel("Trade #", color="white")
    ax.set_ylabel("Cumulative R", color="white")
    ax.tick_params(colors="white")
    for spine in ax.spines.values():
        spine.set_color("#444444")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf