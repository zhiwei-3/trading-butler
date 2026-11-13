import io
import math
import logging
import itertools
import numpy as np
import pandas as pd
import pandas_ta as ta
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta

from config import ALERT_STATE, TIMEFRAME_PRESETS, TF_BY_NAME
from strategies import get_strategy, get_params, STRATEGIES, Params, BacktestData
from core.mt5_engine import MT5_LOCK, get_broker_utc_offset_seconds
from utils.smc import (
    find_swing_points, detect_market_structure,
    detect_liquidity_sweeps, detect_fvg, detect_order_block,
    find_unmitigated_fvgs, fvg_snapshot_from_gaps,
)
from utils.indicators import (
    detect_candlestick_pattern, detect_divergence, find_sr_zones, nearest_sr_zone
)
from utils.chart import CHART_LOCK

MIN_WARMUP_BARS = 210
MAX_BARS_FORWARD = 200
ENTRY_WINDOW = 200
HTF_WINDOW = 200
ATR_AVG_PERIOD = 20
VOLUME_AVG_PERIOD = 20
MIN_INDICATOR_BARS = 80

_TF_MINUTES = {
    mt5.TIMEFRAME_M1: 1, mt5.TIMEFRAME_M5: 5, mt5.TIMEFRAME_M15: 15, mt5.TIMEFRAME_M30: 30,
    mt5.TIMEFRAME_H1: 60, mt5.TIMEFRAME_H4: 240, mt5.TIMEFRAME_D1: 1440,
}

def _min_warmup_days_for(timeframe, min_bars=MIN_INDICATOR_BARS):
    minutes = _TF_MINUTES.get(timeframe, 60)
    calendar_days = (minutes * min_bars) / (60 * 24)
    if minutes >= 1440:
        calendar_days *= 1.5
    return calendar_days

def _fetch_history(symbol, timeframe, start, end):
    with MT5_LOCK:
        rates = mt5.copy_rates_range(symbol, timeframe, start, end)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True).astype('datetime64[ns, UTC]')
    return df

def _precompute_indicators(df):
    df = df.copy()
    df['EMA_20'] = pd.to_numeric(ta.ema(df['close'], length=20), errors='coerce')
    df['EMA_50'] = pd.to_numeric(ta.ema(df['close'], length=50), errors='coerce')
    df['RSI'] = pd.to_numeric(ta.rsi(df['close'], length=14), errors='coerce')
    df['ATR'] = pd.to_numeric(ta.atr(df['high'], df['low'], df['close'], length=14), errors='coerce')
    return df

def _precompute_entry_extras(df):
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
    n = len(times_arr)
    while cursor + 1 < n and times_arr[cursor + 1] <= ts:
        cursor += 1
    return cursor

def _full_analysis_with_memory(entry_slice, state, lookback_bars=10):
    fvg = detect_fvg(entry_slice)
    swing_highs, swing_lows = find_swing_points(entry_slice, state["fractal_window"], state["fractal_window"])
    sweeps = detect_liquidity_sweeps(entry_slice, swing_highs, swing_lows)
    structure = detect_market_structure(entry_slice)

    return {
        "sweeps": sweeps,
        "fvg": fvg,
        "order_block": detect_order_block(entry_slice),
        "structure": structure,
        "macd_bias": entry_slice['MACD_BIAS'].iloc[-1] if 'MACD_BIAS' in entry_slice.columns else None,
        "candle_pattern": detect_candlestick_pattern(entry_slice),
        "divergence": detect_divergence(entry_slice),
        "open_price": float(entry_slice['open'].iloc[-1]),
    }


def _simulate_trade(df_entry, entry_idx, trade, spread_price=0.0, max_bars_forward=MAX_BARS_FORWARD, dual=True):
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
            sim_low, sim_high = low - spread_price, high - spread_price
            if not hit_tp1:
                if sim_low <= sl:
                    return "HIT_SL", times[i]
                if sim_high >= tp1:
                    if not dual:
                        return "HIT_TP1", times[i]      # single leg: fully closed at TP1
                    hit_tp1 = True
                    sl = trade["entry"]
                    if sim_high >= tp2:
                        return "HIT_TP2", times[i]
                    continue
            else:
                if sim_low <= sl:
                    return "HIT_TP1", times[i]
                if sim_high >= tp2:
                    return "HIT_TP2", times[i]
        else:
            sim_low, sim_high = low + spread_price, high + spread_price
            if not hit_tp1:
                if sim_high >= sl:
                    return "HIT_SL", times[i]
                if sim_low <= tp1:
                    if not dual:
                        return "HIT_TP1", times[i]      # single leg: fully closed at TP1
                    hit_tp1 = True
                    sl = trade["entry"]
                    if sim_low <= tp2:
                        return "HIT_TP2", times[i]
                    continue
            else:
                if sim_high >= sl:
                    return "HIT_TP1", times[i]
                if sim_low <= tp2:
                    return "HIT_TP2", times[i]

    return ("HIT_TP1" if hit_tp1 else "OPEN"), times[end_idx]

def _prepare_history(symbol, days, timeframe_mode):
    mode = timeframe_mode or ALERT_STATE["timeframe_mode"]
    if mode not in TIMEFRAME_PRESETS:
        return {"error": f"Unknown timeframe mode '{mode}'"}
    preset = TIMEFRAME_PRESETS[mode]

    # MT5 candle timestamps are broker-SERVER time (epoch labelled as UTC), so the
    # request window must be expressed in server time too.
    offset = timedelta(seconds=get_broker_utc_offset_seconds())
    end = datetime.now(timezone.utc) + offset
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
        return {"error": "Insufficient MT5 historical data returned for the requested range/timeframe."}

    df_entry = _precompute_entry_extras(_precompute_indicators(df_entry_raw))
    df_trend = _precompute_indicators(df_trend_raw)
    df_macro = _precompute_indicators(df_macro_raw)

    if df_trend['EMA_50'].notna().sum() == 0 or df_macro['EMA_50'].notna().sum() == 0:
        return {"error": "Not enough historical bars on the trend/macro timeframe to compute a 50-period EMA."}

    return {"mode": mode, "preset": preset, "symbol": symbol, "start": start, "end": end,
            "df_entry": df_entry, "df_trend": df_trend, "df_macro": df_macro,
            "extra": {},     # lazily filled with the extra timeframes strategies declare in needs.extra
            "memo": {}}      # shared across the runs of one sweep (BacktestData.memo)


def _extra_frame(prepared, name):
    """Raw history for a strategy-requested timeframe (e.g. 'H1'), fetched once and cached."""
    cache = prepared["extra"]
    if name not in cache:
        tf = TF_BY_NAME.get(name)
        if tf is None:
            cache[name] = None
        else:
            warm = max(30, _min_warmup_days_for(tf, 300))
            cache[name] = _fetch_history(prepared["symbol"], tf,
                                         prepared["start"] - timedelta(days=warm), prepared["end"])
    return cache[name]

def run_backtest(symbol, days=30, timeframe_mode=None, spread_pips=2.0, progress_callback=None,
                 strategy_name=None, param_overrides=None, _prepared=None):
    strat = get_strategy(strategy_name or ALERT_STATE.get("active_strategy"))
    if strat is None:
        return {"error": "No strategy selected or installed — add a package to `strategies/` and pick it with `/strategy`."}

    prepared = _prepared or _prepare_history(symbol, days, timeframe_mode)
    if "error" in prepared:
        return prepared

    mode = prepared["mode"]
    preset = prepared["preset"]
    start = prepared["start"]
    df_entry = prepared["df_entry"]
    df_trend = prepared["df_trend"]
    df_macro = prepared["df_macro"]
    needs = strat.needs
    p = Params({**get_params(strat.key), **(param_overrides or {})})     # one-off overrides never touch live settings

    state = {
        "require_structure_break": ALERT_STATE["require_structure_break"],
        "require_volume_atr_filter": ALERT_STATE["require_volume_atr_filter"],
        "fractal_window": ALERT_STATE["fractal_window"],
        "prev_ema_bearish": False,
        "prev_ema_bullish": False,
    }
    state.update(strat.init_state(p))          # the strategy's own dedup/memory keys

    atr_multiplier = ALERT_STATE["atr_multiplier"]
    volume_multiplier = ALERT_STATE["volume_multiplier"]
    spread_price = spread_pips / 10.0

    first_valid_idx = df_entry['ATR_AVG'].first_valid_index() or 0
    test_start_idx = max(MIN_WARMUP_BARS, first_valid_idx, int(df_entry['time'].searchsorted(start, side='left')))
    if test_start_idx >= len(df_entry) - 1:
        return {"error": "Not enough historical bars after warmup to run a backtest over this range."}

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

    entry_minutes = _TF_MINUTES[preset["entry"]]
    entry_td = np.timedelta64(entry_minutes, "m")
    trend_close_arr = trend_time_arr + np.timedelta64(_TF_MINUTES[preset["trend"]], "m")
    macro_close_arr = macro_time_arr + np.timedelta64(_TF_MINUTES[preset["macro"]], "m")

    trend_cursor = max(0, int(np.searchsorted(trend_close_arr, time_arr[test_start_idx] + entry_td, side='right')) - 1)
    macro_cursor = max(0, int(np.searchsorted(macro_close_arr, time_arr[test_start_idx] + entry_td, side='right')) - 1)

    # ---- strategy connector: precompute whatever the strategy needs, once per run
    bt = BacktestData(
        symbol=symbol, entry=df_entry, trend=df_trend, macro=df_macro,
        extra={name: _extra_frame(prepared, name) for name, _bars in needs.extra},
        entry_minutes=entry_minutes, broker_offset_s=int(get_broker_utc_offset_seconds()),
        memo=prepared["memo"],
    )
    try:
        prep = strat.prepare_backtest(bt, p)
    except Exception as e:
        logging.exception(f"Strategy '{strat.key}' prepare_backtest() failed")
        return {"error": f"Strategy `{strat.key}` could not prepare its backtest data: {e}"}

    cached_sr_macro_cursor = -1
    cached_sr_zones = []
    cached_macro_gaps = []

    trades = []
    factor_stats = {}
    equity = [0.0]
    blocked_until = time_arr[0] if len(time_arr) else np.datetime64('1970-01-01T00:00:00')

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
        if ts < blocked_until:
            continue

        rsi_val, close_price, atr_val = rsi_arr[i], close_arr[i], atr_arr[i]
        if np.isnan(rsi_val) or np.isnan(atr_val) or atr_val <= 0:
            continue

        trend_cursor = _advance_cursor(trend_close_arr, trend_cursor, ts + entry_td)
        macro_cursor = _advance_cursor(macro_close_arr, macro_cursor, ts + entry_td)
        if trend_cursor < 1 or macro_cursor < 1:
            continue
        if (np.isnan(trend_ema20_arr[trend_cursor]) or np.isnan(trend_ema50_arr[trend_cursor])
                or np.isnan(macro_ema20_arr[macro_cursor]) or np.isnan(macro_ema50_arr[macro_cursor])):
            continue

        entry_bullish = bool(ema20_arr[i] > ema50_arr[i])
        trend_bullish = bool(trend_ema20_arr[trend_cursor] > trend_ema50_arr[trend_cursor])
        macro_bullish = bool(macro_ema20_arr[macro_cursor] > macro_ema50_arr[macro_cursor])

        if i > 0:
            state["prev_ema_bearish"] = bool(ema20_arr[i-1] <= ema50_arr[i-1])
            state["prev_ema_bullish"] = bool(ema20_arr[i-1] >= ema50_arr[i-1])

        # ---- strategy connector: build this bar's ctx, or skip the bar cheaply (regime/session/RSI gates live here)
        ctx_bar = strat.bar_context(prep, i, p, state)
        if ctx_bar is None:
            continue

        vol_filter_ok = True
        if state["require_volume_atr_filter"] or needs.vol_filter:
            vol_filter_ok = _vol_filter_ok(df_entry.iloc[i], atr_multiplier, volume_multiplier)
            if state["require_volume_atr_filter"] and not vol_filter_ok:
                continue

        if needs.standard_analysis:
            entry_slice = df_entry.iloc[max(0, i - (ENTRY_WINDOW - 1)):i + 1]
            if macro_cursor != cached_sr_macro_cursor:
                macro_slice = df_macro.iloc[max(0, macro_cursor - (HTF_WINDOW - 1)):macro_cursor + 1].reset_index(drop=True)
                cached_sr_zones = find_sr_zones(
                    macro_slice,
                    lookback=ALERT_STATE["sr_lookback"],
                    cluster_pct=ALERT_STATE["sr_cluster_pct"],
                    min_touches=ALERT_STATE["sr_min_touches"],
                )
                cached_macro_gaps = find_unmitigated_fvgs(macro_slice)
                cached_sr_macro_cursor = macro_cursor
            full = _full_analysis_with_memory(entry_slice, state)
            full["sr_zones"] = cached_sr_zones
            full["macro_fvg"] = fvg_snapshot_from_gaps(cached_macro_gaps, float(close_price))
        else:
            full = {}
        full["vol_filter_ok"] = vol_filter_ok
        full["ctx"] = ctx_bar

        price_r = round(float(close_price), 2)
        c = {
            "close_price": price_r, "rsi_val": round(float(rsi_val), 2), "atr_val": round(float(atr_val), 2),
            "entry_bullish": entry_bullish, "trend_bullish": trend_bullish, "macro_bullish": macro_bullish,
            "full": full, "ctx": ctx_bar, "state": state, "p": p,
            "structure": full.get("structure", "NEUTRAL"),
            "near_zone": nearest_sr_zone(full.get("sr_zones", []), price_r,
                                         max_distance_pct=ALERT_STATE["sr_max_distance_pct"]),
        }
        trade = strat.decide(c)
        if trade is None:
            continue

        is_dual = strat.is_dual(trade["score"], p)
        outcome, close_time = _simulate_trade(df_entry, i, trade, spread_price=spread_price, dual=is_dual)
        blocked_until = close_time
        if outcome == "HIT_SL" and ALERT_STATE.get("sl_cooldown_enabled", True):
            cd_mins = int(ALERT_STATE.get("sl_cooldown_minutes", 30))
            if cd_mins > 0:
                blocked_until = close_time + np.timedelta64(cd_mins, "m")

        tp1_r = trade["tp1_dist"] / trade["sl_dist"] if trade["sl_dist"] > 0 else 0.0
        tp2_r = trade["tp2_dist"] / trade["sl_dist"] if trade["sl_dist"] > 0 else 0.0

        if outcome == "HIT_SL":
            r_multiple = -1.0
        elif outcome == "OPEN":
            r_multiple = 0.0
        elif not is_dual:
            r_multiple = round(tp1_r, 2)                    # single leg closes at TP1
        elif outcome == "HIT_TP1":
            r_multiple = round(tp1_r / 2, 2)                # leg1 TP1 + runner BE
        else:                                               # HIT_TP2
            r_multiple = round((tp1_r + tp2_r) / 2, 2)

        trades.append({
            "time": ts, "direction": trade["direction"], "entry": trade["entry"],
            "sl": trade["sl_price"], "tp1": trade["tp1_price"], "tp2": trade["tp2_price"],
            "score": trade["score"], "outcome": outcome, "r_multiple": r_multiple,
            "closed_at": close_time,
        })
        equity.append(round(equity[-1] + r_multiple, 2))

        won = outcome in ("HIT_TP1", "HIT_TP2")
        if outcome in ("HIT_TP1", "HIT_TP2", "HIT_SL"):
            for label, earned, possible in trade.get("breakdown", []):
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
        "symbol": symbol, "mode": mode, "days": days, "strategy": strat.key,
        "params": dict(p),
        "total_trades": total, "wins": wins, "losses": losses, "open": still_open,
        "win_rate": win_rate, "avg_r": avg_r, "net_r": net_r, "max_drawdown_r": round(max_dd, 2),
        "equity_curve": equity, "trades": trades, "factor_summary": factor_summary,
    }

MAX_SWEEP_COMBOS = 150
MIN_SWEEP_SAMPLE = 5

def run_backtest_sweep(symbol, days=30, timeframe_mode=None, plan=None, spread_pips=2.0, progress_callback=None):
    """plan: [(strategy_key, {param_key: [values]})] from strategies.plan_sweep()."""
    if not STRATEGIES:
        return {"error": "No strategies installed — add a package to `strategies/` first."}

    combos = []
    for skey, axes in (plan or []):
        strat = STRATEGIES.get(skey)
        if strat is None:
            continue
        base = get_params(skey)
        names = list(axes)
        for vals in itertools.product(*(axes[n] for n in names)):
            over = dict(zip(names, vals))
            if strat.valid_params(Params({**base, **over})):
                combos.append((skey, over))

    if not combos:
        return {"error": "The sweep grid is empty (every combination was invalid)."}
    if len(combos) > MAX_SWEEP_COMBOS:
        return {"error": f"Sweep would run {len(combos)} backtests (cap is {MAX_SWEEP_COMBOS}). "
                          f"Sweep fewer dimensions at once."}

    prepared = _prepare_history(symbol, days, timeframe_mode)
    if "error" in prepared:
        return {"error": prepared["error"]}

    results = []
    for idx, (skey, over) in enumerate(combos):
        if progress_callback:
            try:
                progress_callback(int(idx / len(combos) * 100))
            except Exception:
                pass

        res = run_backtest(symbol, days=days, timeframe_mode=timeframe_mode, spread_pips=spread_pips,
                           strategy_name=skey, param_overrides=over, _prepared=prepared)
        row = {"strategy": skey, "params": {**get_params(skey), **over}}
        if "error" in res:
            row["error"] = res["error"]
        else:
            row.update({
                "total_trades": res["total_trades"], "wins": res["wins"], "losses": res["losses"],
                "open": res["open"], "win_rate": res["win_rate"], "avg_r": res["avg_r"],
                "net_r": res["net_r"], "max_drawdown_r": res["max_drawdown_r"],
            })
        results.append(row)

    if progress_callback:
        try:
            progress_callback(100)
        except Exception:
            pass

    axes_swept = []
    if len({s for s, _ in plan}) > 1:
        axes_swept.append("strategies")
    for _, axes in plan:
        for k in axes:
            if k not in axes_swept:
                axes_swept.append(k)

    return {
        "symbol": symbol, "mode": timeframe_mode, "days": days, "grid": results,
        "axes": axes_swept,
        "min_sample": max(MIN_SWEEP_SAMPLE, math.ceil(days * 0.5)),
    }

def generate_equity_chart(equity_curve, title="Backtest Equity Curve"):
    if not equity_curve or len(equity_curve) < 2:
        return None
    with CHART_LOCK:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
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