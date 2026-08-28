import pandas_ta as ta
from config import ALERT_STATE, TIMEFRAME_PRESETS, CONFLUENCE_WEIGHTS
from database import log_signal_to_db
from mt5_engine import fetch_candles
from strategy.smc import (
    find_swing_points, detect_market_structure, 
    detect_liquidity_sweeps, detect_fvg, detect_order_block
)
from strategy.indicators import (
    passes_volatility_filter, get_macd_bias, detect_candlestick_pattern,
    detect_divergence, find_sr_zones, nearest_sr_zone
)

def _eval_ema_cross(a):
    df = a["df_entry"]
    close_price, atr_val = a["close_price"], a["atr_val"]
    ema20, ema50 = df['EMA_20'].iloc[-1], df['EMA_50'].iloc[-1]
    prev_ema20, prev_ema50 = df['EMA_20'].iloc[-2], df['EMA_50'].iloc[-2]

    signals, watches = [], []
    sl_dist, tp1_dist = round(atr_val * 1.5, 2), round(atr_val * 2.5, 2)

    bullish_cross = (prev_ema20 <= prev_ema50 and ema20 > ema50)
    bearish_cross = (prev_ema20 >= prev_ema50 and ema20 < ema50)

    if bullish_cross and ALERT_STATE["last_rsi_signal"] != "BUY":
        ALERT_STATE["last_rsi_signal"] = "BUY"
        sl_price, tp1_price = round(close_price - sl_dist, 2), round(close_price + tp1_dist, 2)
        log_signal_to_db("XAUUSD", "BUY", close_price, sl_price, tp1_price, round(close_price + atr_val * 3.5, 2), 70)
        signals.append(f"🏆 **EMA CROSS BUY ALERT** 🟢\n\n📍 **Entry:** `${close_price}` | 🛡️ **SL:** `${sl_price}` | 🎯 **TP1:** `${tp1_price}`")

    elif bearish_cross and ALERT_STATE["last_rsi_signal"] != "SELL":
        ALERT_STATE["last_rsi_signal"] = "SELL"
        sl_price, tp1_price = round(close_price + sl_dist, 2), round(close_price - tp1_dist, 2)
        log_signal_to_db("XAUUSD", "SELL", close_price, sl_price, tp1_price, round(close_price - atr_val * 3.5, 2), 70)
        signals.append(f"🏆 **EMA CROSS SELL ALERT** 🔴\n\n📍 **Entry:** `${close_price}` | 🛡️ **SL:** `${sl_price}` | 🎯 **TP1:** `${tp1_price}`")

    if not (bullish_cross or bearish_cross):
        ALERT_STATE["last_rsi_signal"] = None

    return signals, watches

def _eval_rsi_reversion(a):
    """RSI Overbought/Oversold Reversion at S/R Zones."""
    close_price, rsi_val, atr_val = a["close_price"], a["rsi_val"], a["atr_val"]
    near_zone, order_block = a["near_zone"], a["order_block"]

    buy_th = ALERT_STATE.get("rsi_buy_threshold", 30)
    sell_th = ALERT_STATE.get("rsi_sell_threshold", 70)

    signals, watches = [], []

    # Oversold + Support Zone Bounce
    bullish_rev = (
        rsi_val <= buy_th and 
        near_zone and near_zone["type"] in ("support", "mixed") and 
        close_price >= near_zone["price"]
    )

    # Overbought + Resistance Zone Rejection
    bearish_rev = (
        rsi_val >= sell_th and 
        near_zone and near_zone["type"] in ("resistance", "mixed") and 
        close_price <= near_zone["price"]
    )

    if bullish_rev and ALERT_STATE["last_rsi_signal"] != "BUY":
        targets = calculate_targets("BUY", close_price, atr_val, order_block, near_zone)
        if targets["rrr"] >= ALERT_STATE.get("min_rrr", 1.0):
            ALERT_STATE["last_rsi_signal"] = "BUY"
            sl_price, tp1_price, tp2_price = targets["sl_price"], targets["tp1_price"], targets["tp2_price"]
            log_signal_to_db("XAUUSD", "BUY", close_price, sl_price, tp1_price, tp2_price, 75)
            signals.append(f"🔄 **RSI REVERSION BUY ALERT** 🟢\n\n📍 **Entry:** `${close_price}` | 🛡️ **SL:** `${sl_price}` | 🎯 **TP1:** `${tp1_price}`")

    elif bearish_rev and ALERT_STATE["last_rsi_signal"] != "SELL":
        targets = calculate_targets("SELL", close_price, atr_val, order_block, near_zone)
        if targets["rrr"] >= ALERT_STATE.get("min_rrr", 1.0):
            ALERT_STATE["last_rsi_signal"] = "SELL"
            sl_price, tp1_price, tp2_price = targets["sl_price"], targets["tp1_price"], targets["tp2_price"]
            log_signal_to_db("XAUUSD", "SELL", close_price, sl_price, tp1_price, tp2_price, 75)
            signals.append(f"🔄 **RSI REVERSION SELL ALERT** 🔴\n\n📍 **Entry:** `${close_price}` | 🛡️ **SL:** `${sl_price}` | 🎯 **TP1:** `${tp1_price}`")

    if not (bullish_rev or bearish_rev):
        ALERT_STATE["last_rsi_signal"] = None

    return signals, watches

def _eval_smc_displacement(a):
    df = a["df_entry"]
    close_price, atr_val = a["close_price"], a["atr_val"]
    structure, fvg, order_block = a["structure"], a["fvg"], a["order_block"]
    near_zone = a["near_zone"]

    signals, watches = [], []

    last_candle = df.iloc[-1]
    candle_body = abs(last_candle['close'] - last_candle['open'])
    # Standardized ATR body multiplier to 1.0x
    has_displacement = candle_body >= (atr_val * 1.0)

    bullish_disp = (
        has_displacement and 
        last_candle['close'] > last_candle['open'] and 
        (structure == "BULLISH_BOS" or fvg.get("bullish_fvg", False)) and
        (a["trend_bullish"] or a["macro_bullish"])
    )

    bearish_disp = (
        has_displacement and 
        last_candle['close'] < last_candle['open'] and 
        (structure == "BEARISH_BOS" or fvg.get("bearish_fvg", False)) and
        ((not a["trend_bullish"]) or (not a["macro_bullish"]))
    )

    if bullish_disp and ALERT_STATE["last_rsi_signal"] != "BUY":
        targets = calculate_targets("BUY", close_price, atr_val, order_block, near_zone)
        if targets["rrr"] >= ALERT_STATE.get("min_rrr", 1.3):
            ALERT_STATE["last_rsi_signal"] = "BUY"
            sl_price, tp1_price, tp2_price = targets["sl_price"], targets["tp1_price"], targets["tp2_price"]
            log_signal_to_db("XAUUSD", "BUY", close_price, sl_price, tp1_price, tp2_price, 85)
            signals.append(
                f"⚡ **SMC DISPLACEMENT BUY ALERT** 🟢\n\n"
                f"📍 **Entry:** `${close_price}` | 🛡️ **SL:** `${sl_price}` | 🎯 **TP1:** `${tp1_price}`"
            )

    elif bearish_disp and ALERT_STATE["last_rsi_signal"] != "SELL":
        targets = calculate_targets("SELL", close_price, atr_val, order_block, near_zone)
        if targets["rrr"] >= ALERT_STATE.get("min_rrr", 1.3):
            ALERT_STATE["last_rsi_signal"] = "SELL"
            sl_price, tp1_price, tp2_price = targets["sl_price"], targets["tp1_price"], targets["tp2_price"]
            log_signal_to_db("XAUUSD", "SELL", close_price, sl_price, tp1_price, tp2_price, 85)
            signals.append(
                f"⚡ **SMC DISPLACEMENT SELL ALERT** 🔴\n\n"
                f"📍 **Entry:** `${close_price}` | 🛡️ **SL:** `${sl_price}` | 🎯 **TP1:** `${tp1_price}`"
            )

    if not (bullish_disp or bearish_disp):
        ALERT_STATE["last_rsi_signal"] = None

    return signals, watches

def _eval_htf_fvg_ltf_sweep(a):
    close_price, atr_val = a["close_price"], a["atr_val"]
    macro_fvg = a.get("macro_fvg", {})
    fvg, sweeps, structure = a["fvg"], a["sweeps"], a["structure"]
    order_block, near_zone = a["order_block"], a["near_zone"]

    fvg_top = macro_fvg.get("fvg_top", 0)
    fvg_bottom = macro_fvg.get("fvg_bottom", 0)

    # Added explicit price boundary retest validation
    htf_bull_tap = macro_fvg.get("bullish_fvg", False) and (fvg_bottom <= close_price <= fvg_top if fvg_top > 0 else True)
    htf_bear_tap = macro_fvg.get("bearish_fvg", False) and (fvg_bottom <= close_price <= fvg_top if fvg_top > 0 else True)

    bullish_setup = (
        htf_bull_tap and
        (sweeps.get("bullish_sweep", False) or structure == "BULLISH_BOS") and
        fvg.get("bullish_fvg", False)
    )

    bearish_setup = (
        htf_bear_tap and
        (sweeps.get("bearish_sweep", False) or structure == "BEARISH_BOS") and
        fvg.get("bearish_fvg", False)
    )

    signals, watches = [], []

    if bullish_setup and ALERT_STATE["last_rsi_signal"] != "BUY":
        targets = calculate_targets("BUY", close_price, atr_val, order_block, near_zone)
        if targets["rrr"] >= ALERT_STATE.get("min_rrr", 1.3):
            ALERT_STATE["last_rsi_signal"] = "BUY"
            sl_price, tp1_price, tp2_price = targets["sl_price"], targets["tp1_price"], targets["tp2_price"]
            log_signal_to_db("XAUUSD", "BUY", close_price, sl_price, tp1_price, tp2_price, 90)
            signals.append(
                f"🎯 **MTF FVG SWEEP BUY ALERT** 🟢\n\n"
                f"📍 **Entry:** `${close_price}` | 🛡️ **SL:** `${sl_price}` | 🎯 **TP1:** `${tp1_price}`"
            )

    elif bearish_setup and ALERT_STATE["last_rsi_signal"] != "SELL":
        targets = calculate_targets("SELL", close_price, atr_val, order_block, near_zone)
        if targets["rrr"] >= ALERT_STATE.get("min_rrr", 1.3):
            ALERT_STATE["last_rsi_signal"] = "SELL"
            sl_price, tp1_price, tp2_price = targets["sl_price"], targets["tp1_price"], targets["tp2_price"]
            log_signal_to_db("XAUUSD", "SELL", close_price, sl_price, tp1_price, tp2_price, 90)
            signals.append(
                f"🎯 **MTF FVG SWEEP SELL ALERT** 🔴\n\n"
                f"📍 **Entry:** `${close_price}` | 🛡️ **SL:** `${sl_price}` | 🎯 **TP1:** `${tp1_price}`"
            )

    if not (bullish_setup or bearish_setup):
        ALERT_STATE["last_rsi_signal"] = None

    return signals, watches

def score_label(score):
    if score >= 80: return "🔥 Very Strong"
    if score >= 65: return "✅ Strong"
    if score >= 50: return "⚠️ Moderate"
    return "❌ Weak"

def compute_confluence_score(direction, rsi_val, structure, sweeps, fvg, vol_filter_ok, macd_bias,
                              candle_pattern, divergence, sr_confluence, entry_bullish, macro_aligned):
    breakdown = []
    
    # 1. RSI Zone
    rsi_pts = CONFLUENCE_WEIGHTS["rsi_zone"] if (rsi_val <= 20 if direction == "BUY" else rsi_val >= 80) else (10 if (rsi_val <= 25 if direction == "BUY" else rsi_val >= 75) else 6)
    breakdown.append(("RSI Zone", rsi_pts, CONFLUENCE_WEIGHTS["rsi_zone"]))

    # 2. EMA Trend Alignment (FIXED: Checks entry_bullish directionally)
    ema_match = (direction == "BUY" and entry_bullish) or (direction == "SELL" and not entry_bullish)
    breakdown.append(("EMA Trend Align", CONFLUENCE_WEIGHTS["ema_trend"] if ema_match else 0, CONFLUENCE_WEIGHTS["ema_trend"]))

    # 3. Macro Trend Alignment
    breakdown.append(("Macro Trend Align", CONFLUENCE_WEIGHTS["macro_trend"] if macro_aligned else 0, CONFLUENCE_WEIGHTS["macro_trend"]))

    # 4. Structure BOS
    bos_match = (direction == "BUY" and structure == "BULLISH_BOS") or (direction == "SELL" and structure == "BEARISH_BOS")
    breakdown.append(("Structure BOS", CONFLUENCE_WEIGHTS["structure_bos"] if bos_match else 0, CONFLUENCE_WEIGHTS["structure_bos"]))

    # 5. Liquidity Sweep
    sweep_match = (direction == "BUY" and sweeps["bullish_sweep"]) or (direction == "SELL" and sweeps["bearish_sweep"])
    breakdown.append(("Liquidity Sweep", CONFLUENCE_WEIGHTS["liquidity_sweep"] if sweep_match else 0, CONFLUENCE_WEIGHTS["liquidity_sweep"]))

    # 6. Fair Value Gap
    fvg_match = (direction == "BUY" and fvg["bullish_fvg"]) or (direction == "SELL" and fvg["bearish_fvg"])
    breakdown.append(("Fair Value Gap", CONFLUENCE_WEIGHTS["fvg"] if fvg_match else 0, CONFLUENCE_WEIGHTS["fvg"]))

    # 7. Volume/ATR Filter
    breakdown.append(("Volume/ATR Activity", CONFLUENCE_WEIGHTS["volume_atr"] if vol_filter_ok else 0, CONFLUENCE_WEIGHTS["volume_atr"]))

    # 8. MACD Bias
    macd_match = (direction == "BUY" and macd_bias == "BULLISH") or (direction == "SELL" and macd_bias == "BEARISH")
    breakdown.append(("MACD Confirmation", CONFLUENCE_WEIGHTS["macd"] if macd_match else 0, CONFLUENCE_WEIGHTS["macd"]))

    # 9. Candlestick Pattern
    pattern_match = (direction == "BUY" and candle_pattern in ("BULLISH_ENGULFING", "HAMMER")) or (direction == "SELL" and candle_pattern in ("BEARISH_ENGULFING", "SHOOTING_STAR"))
    breakdown.append(("Candlestick Pattern", CONFLUENCE_WEIGHTS["candlestick"] if pattern_match else (3 if candle_pattern == "DOJI" else 0), CONFLUENCE_WEIGHTS["candlestick"]))

    # 10. RSI Divergence
    div_match = (direction == "BUY" and divergence.get("bullish")) or (direction == "SELL" and divergence.get("bearish"))
    breakdown.append(("RSI Divergence", CONFLUENCE_WEIGHTS["divergence"] if div_match else 0, CONFLUENCE_WEIGHTS["divergence"]))

    # 11. Support / Resistance Zone
    breakdown.append(("Support/Resistance", CONFLUENCE_WEIGHTS["sr_zone"] if sr_confluence else 0, CONFLUENCE_WEIGHTS["sr_zone"]))

    return sum(pts for _, pts, _ in breakdown), breakdown

def format_confluence_breakdown(score, breakdown):
    lines = [f"📊 **Confluence Score: {score}/100** ({score_label(score)})"]
    for label, earned, possible in breakdown:
        check = "✅" if earned == possible and possible > 0 else ("🟡" if earned > 0 else "❌")
        lines.append(f"  {check} {label}: `{earned}/{possible}`")
    return "\n".join(lines)

def calculate_targets(direction, close_price, atr_val, order_block, near_zone):
    sl_mult = ALERT_STATE["sl_atr_mult"]
    tp1_mult = ALERT_STATE["tp1_atr_mult"]
    tp2_mult = ALERT_STATE["tp2_atr_mult"]
    min_dist = atr_val * 0.5
    ob_buffer = atr_val * 0.25

    if direction == "BUY":
        sl_price, sl_source = close_price - atr_val * sl_mult, "ATR"
        if order_block.get("bullish_ob") and order_block.get("ob_level") is not None:
            ob_sl = order_block["ob_level"] - ob_buffer
            if ob_sl < close_price and (close_price - ob_sl) >= min_dist:
                sl_price, sl_source = ob_sl, "Order Block"

        tp1_price, tp1_source = close_price + atr_val * tp1_mult, "ATR"
        if near_zone and near_zone["type"] in ("resistance", "mixed") and near_zone["price"] > close_price and (near_zone["price"] - close_price) >= min_dist:
            tp1_price, tp1_source = near_zone["price"], "S/R Zone"

        tp2_price = max(close_price + atr_val * tp2_mult, tp1_price + atr_val * 0.5)

    else:  # SELL
        sl_price, sl_source = close_price + atr_val * sl_mult, "ATR"
        if order_block.get("bearish_ob") and order_block.get("ob_level") is not None:
            ob_sl = order_block["ob_level"] + ob_buffer
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

def analyze_market(symbol):
    entry_tf, trend_tf, macro_tf = ALERT_STATE["entry_tf"], ALERT_STATE["trend_tf"], ALERT_STATE["macro_tf"]
    df_entry = fetch_candles(symbol, entry_tf, 200)
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
    order_block = detect_order_block(df_entry)
    vol_filter_ok, _ = passes_volatility_filter(df_entry)
    sr_zones = find_sr_zones(df_macro)
    macro_fvg = detect_fvg(df_macro)

    return {
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
        "macro_fvg": macro_fvg,  # Added HTF FVG detection
        "order_block": order_block,
        "vol_filter_ok": vol_filter_ok,
        "macd_bias": get_macd_bias(df_entry),
        "candle_pattern": detect_candlestick_pattern(df_entry),
        "divergence": detect_divergence(df_entry),
        "near_zone": nearest_sr_zone(sr_zones, close_price),
    }

def evaluate_signals(a):
    strat = ALERT_STATE.get("active_strategy", "smc_confluence")
    if strat == "htf_fvg_sweep":
        return _eval_htf_fvg_ltf_sweep(a)
    elif strat == "smc_displacement":
        return _eval_smc_displacement(a)
    elif strat == "ema_cross":
        return _eval_ema_cross(a)
    elif strat == "rsi_reversion":
        return _eval_rsi_reversion(a)
    else:
        return evaluate_smc_confluence(a)

def evaluate_smc_confluence(a):
    close_price, rsi_val, atr_val = a["close_price"], a["rsi_val"], a["atr_val"]
    entry_bullish, trend_bullish, macro_bullish = a["entry_bullish"], a["trend_bullish"], a["macro_bullish"]
    buy_th, sell_th = ALERT_STATE["rsi_buy_threshold"], ALERT_STATE["rsi_sell_threshold"]
    min_score = ALERT_STATE["min_confluence_score"]

    signals_found, watch_found = [], []

    buy_structure_ok = (not ALERT_STATE["require_structure_break"]) or (a["structure"] == "BULLISH_BOS")
    sell_structure_ok = (not ALERT_STATE["require_structure_break"]) or (a["structure"] == "BEARISH_BOS")
    vol_filter_hard_ok = a["vol_filter_ok"] if ALERT_STATE["require_volume_atr_filter"] else True
    in_approach_zone = (
        (buy_th < rsi_val <= buy_th + ALERT_STATE["watch_rsi_margin"]) or
        (sell_th - ALERT_STATE["watch_rsi_margin"] <= rsi_val < sell_th)
    )

    if rsi_val <= buy_th and (trend_bullish or macro_bullish) and buy_structure_ok and vol_filter_hard_ok:
        sr_confluence = bool(a["near_zone"] and a["near_zone"]["type"] in ("support", "mixed") and close_price >= a["near_zone"]["price"])
        score, breakdown = compute_confluence_score(
            "BUY", rsi_val, a["structure"], a["sweeps"], a["fvg"], 
            a["vol_filter_ok"], a["macd_bias"], a["candle_pattern"], 
            a["divergence"], sr_confluence, entry_bullish, macro_bullish
        )

        if score >= min_score and ALERT_STATE["last_rsi_signal"] != "BUY":
            targets = calculate_targets("BUY", close_price, atr_val, a["order_block"], a["near_zone"])
            min_rrr_target = ALERT_STATE.get("min_rrr", 1.3)

            if targets["rrr"] >= min_rrr_target:
                ALERT_STATE["last_rsi_signal"] = "BUY"
                sl_price, tp1_price, tp2_price, rrr = targets["sl_price"], targets["tp1_price"], targets["tp2_price"], targets["rrr"]
                log_signal_to_db("XAUUSD", "BUY", close_price, sl_price, tp1_price, tp2_price, score)

                msg = (
                    f"🏆 **TRADING BUTLER SIGNAL ALERT** 🏆\n\n"
                    f"• **Type:** `BUY 🟢` | **Timeframe:** {a['tf_label']}\n"
                    f"📍 **Entry:** `${close_price}` | 📊 **14-ATR:** `${atr_val}`\n"
                    f"🛡️ **SL:** `${sl_price}` ({targets['sl_source']}, {targets['sl_dist']} pts)\n"
                    f"🎯 **TP1:** `${tp1_price}` ({targets['tp1_source']}) | 🎯 **TP2:** `${tp2_price}` (RRR: `1:{rrr}`)\n\n"
                    f"{format_confluence_breakdown(score, breakdown)}\n\n"
                    f"💧 **Sweep:** {'Bullish Sweep ✅' if a['sweeps']['bullish_sweep'] else 'None'}\n"
                    f"⚡ **FVG:** {'Bullish FVG ✅' if a['fvg']['bullish_fvg'] else 'None'}\n"
                    f"🧱 **Order Block:** {'Bullish OB ✅ @ $' + str(a['order_block']['ob_level']) if a['order_block']['bullish_ob'] else 'None'}"
                )
                signals_found.append(msg)

    elif rsi_val >= sell_th and ((not trend_bullish) or (not macro_bullish)) and sell_structure_ok and vol_filter_hard_ok:
        sr_confluence = bool(a["near_zone"] and a["near_zone"]["type"] in ("resistance", "mixed") and close_price <= a["near_zone"]["price"])
        score, breakdown = compute_confluence_score(
            "SELL", rsi_val, a["structure"], a["sweeps"], a["fvg"], 
            a["vol_filter_ok"], a["macd_bias"], a["candle_pattern"], 
            a["divergence"], sr_confluence, entry_bullish, not macro_bullish
        )

        if score >= min_score and ALERT_STATE["last_rsi_signal"] != "SELL":
            targets = calculate_targets("SELL", close_price, atr_val, a["order_block"], a["near_zone"])
            min_rrr_target = ALERT_STATE.get("min_rrr", 1.3)

            if targets["rrr"] >= min_rrr_target:
                ALERT_STATE["last_rsi_signal"] = "SELL"
                sl_price, tp1_price, tp2_price, rrr = targets["sl_price"], targets["tp1_price"], targets["tp2_price"], targets["rrr"]
                log_signal_to_db("XAUUSD", "SELL", close_price, sl_price, tp1_price, tp2_price, score)

                msg = (
                    f"🏆 **TRADING BUTLER SIGNAL ALERT** 🏆\n\n"
                    f"• **Type:** `SELL 🔴` | **Timeframe:** {a['tf_label']}\n"
                    f"📍 **Entry:** `${close_price}` | 📊 **14-ATR:** `${atr_val}`\n"
                    f"🛡️ **SL:** `${sl_price}` ({targets['sl_source']}, {targets['sl_dist']} pts)\n"
                    f"🎯 **TP1:** `${tp1_price}` ({targets['tp1_source']}) | 🎯 **TP2:** `${tp2_price}` (RRR: `1:{rrr}`)\n\n"
                    f"{format_confluence_breakdown(score, breakdown)}\n\n"
                    f"💧 **Sweep:** {'Bearish Sweep ✅' if a['sweeps']['bearish_sweep'] else 'None'}\n"
                    f"⚡ **FVG:** {'Bearish FVG ✅' if a['fvg']['bearish_fvg'] else 'None'}\n"
                    f"🧱 **Order Block:** {'Bearish OB ✅ @ $' + str(a['order_block']['ob_level']) if a['order_block']['bearish_ob'] else 'None'}"
                )
                signals_found.append(msg)

    elif ALERT_STATE["setup_forming_enabled"] and in_approach_zone:
        if ALERT_STATE["last_watch_signal"] is None:
            ALERT_STATE["last_watch_signal"] = "FORMING"
            watch_msg = (
                f"👀 **SETUP FORMING (EARLY HEADS-UP)** 👀\n\n"
                f"• **Symbol:** `XAUUSD` | **Price:** `${close_price}`\n"
                f"• **Current RSI:** `{rsi_val}` (Approaching Zone: `{buy_th}` / `{sell_th}`)\n"
                f"💡 *Monitor charts for imminent breakout or rejection.*"
            )
            watch_found.append(watch_msg)

    else:
        ALERT_STATE["last_rsi_signal"] = None

    if not in_approach_zone:
        ALERT_STATE["last_watch_signal"] = None

    return signals_found, watch_found