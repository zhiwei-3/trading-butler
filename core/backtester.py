import math
import logging
import itertools
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

from config import ALERT_STATE, TIMEFRAME_PRESETS, TF_MINUTES
from core.mt5 import mt5, fetch_history, get_broker_utc_offset_seconds
from analysis import add_indicators, compute, View, default_config
from strategies import get_strategy, get_params, STRATEGIES, Params, BacktestData

MIN_WARMUP_BARS = 210
MAX_BARS_FORWARD = 200
ENTRY_WINDOW = 200
HTF_WINDOW = 200
MIN_INDICATOR_BARS = 80
MAX_SWEEP_COMBOS = 150
MIN_SWEEP_SAMPLE = 5


def _min_warmup_days_for(timeframe, min_bars=MIN_INDICATOR_BARS):
    minutes = TF_MINUTES.get(timeframe, 60)
    calendar_days = (minutes * min_bars) / (60 * 24)
    if minutes >= 1440:
        calendar_days *= 1.5
    return calendar_days


def _advance_cursor(times_arr, cursor, ts):
    n = len(times_arr)
    while cursor + 1 < n and times_arr[cursor + 1] <= ts:
        cursor += 1
    return cursor


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

    df_entry_raw = fetch_history(symbol, preset["entry"], warmup_start, end)
    df_trend_raw = fetch_history(symbol, preset["trend"], warmup_start, end)
    df_macro_raw = fetch_history(symbol, preset["macro"], warmup_start, end)
    if df_entry_raw is None or df_trend_raw is None or df_macro_raw is None:
        return {"error": "Insufficient MT5 historical data returned for the requested range/timeframe."}

    df_entry = add_indicators(df_entry_raw, extras=True)      # same builder the live path uses
    df_trend = add_indicators(df_trend_raw)
    df_macro = add_indicators(df_macro_raw)

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
        if name not in TF_MINUTES:
            cache[name] = None
        else:
            warm = max(30, _min_warmup_days_for(name, 300))
            cache[name] = fetch_history(prepared["symbol"], name,
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
        "fractal_window": ALERT_STATE["fractal_window"],
        "prev_ema_bearish": False,
        "prev_ema_bullish": False,
    }
    state.update(strat.init_state(p))

    gate_vol = bool(p.get("require_volume", False))
    feature_names = tuple(set(needs.features) | ({"vol_filter_ok"} if gate_vol else set()))
    cfg = default_config()
    macro_store = {}                           # macro-scope features are recomputed only when a macro bar closes
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

    entry_minutes = TF_MINUTES[preset["entry"]]
    entry_td = np.timedelta64(entry_minutes, "m")
    trend_close_arr = trend_time_arr + np.timedelta64(TF_MINUTES[preset["trend"]], "m")
    macro_close_arr = macro_time_arr + np.timedelta64(TF_MINUTES[preset["macro"]], "m")

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

        price_r, rsi_r, atr_r = round(float(close_price), 2), round(float(rsi_val), 2), round(float(atr_val), 2)

        # ---- the SAME analysis pipeline the live scanner uses (slices are lazy: only built if a feature needs them)
        mc = macro_cursor
        view = View(
            entry=lambda i=i: df_entry.iloc[max(0, i - (ENTRY_WINDOW - 1)):i + 1],
            price=price_r, atr=atr_r, cfg=cfg,
            macro=lambda mc=mc: df_macro.iloc[max(0, mc - (HTF_WINDOW - 1)):mc + 1].reset_index(drop=True),
            macro_store=macro_store, macro_key=mc,
        )
        if gate_vol and not view.get("vol_filter_ok"):
            continue
        values = compute(feature_names, view)

        a = {
            "symbol": symbol, "close_price": price_r, "rsi_val": rsi_r, "atr_val": atr_r,
            "entry_bullish": entry_bullish, "trend_bullish": trend_bullish, "macro_bullish": macro_bullish,
            "df_entry": view.entry, **values, "ctx": ctx_bar,
        }
        c = {
            "a": a,                                   # same shape as the live analysis dict
            "close_price": price_r, "rsi_val": rsi_r, "atr_val": atr_r,
            "entry_bullish": entry_bullish, "trend_bullish": trend_bullish, "macro_bullish": macro_bullish,
            "full": values, "ctx": ctx_bar, "state": state, "p": p,
            "structure": values.get("structure"), "near_zone": values.get("near_zone"),
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
        "symbol": symbol, "mode": mode, "days": days, "strategy": strat.key, "params": dict(p),
        "total_trades": total, "wins": wins, "losses": losses, "open": still_open,
        "win_rate": win_rate, "avg_r": avg_r, "net_r": net_r, "max_drawdown_r": round(max_dd, 2),
        "equity_curve": equity, "trades": trades, "factor_summary": factor_summary,
    }


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
