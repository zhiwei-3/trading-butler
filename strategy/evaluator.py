import pandas_ta as ta
from config import ALERT_STATE, TIMEFRAME_PRESETS, CONFLUENCE_WEIGHTS
from database import log_signal_to_db
from mt5_engine import fetch_candles
from strategy.smc import find_swing_points, detect_market_structure, detect_liquidity_sweeps, detect_fvg
from strategy.indicators import (
    passes_volatility_filter, get_macd_bias, detect_candlestick_pattern,
    detect_divergence, find_sr_zones, nearest_sr_zone
)

def score_label(score):
    if score >= 80: return "🔥 Very Strong"
    if score >= 65: return "✅ Strong"
    if score >= 50: return "⚠️ Moderate"
    return "❌ Weak"

def compute_confluence_score(direction, rsi_val, structure, sweeps, fvg, vol_filter_ok, macd_bias,
                              candle_pattern, divergence, sr_confluence, macro_aligned):
    breakdown = []
    rsi_pts = CONFLUENCE_WEIGHTS["rsi_zone"] if (rsi_val <= 20 if direction == "BUY" else rsi_val >= 80) else (10 if (rsi_val <= 25 if direction == "BUY" else rsi_val >= 75) else 6)
    breakdown.append(("RSI Zone", rsi_pts, CONFLUENCE_WEIGHTS["rsi_zone"]))
    breakdown.append(("EMA Trend Align", CONFLUENCE_WEIGHTS["ema_trend"], CONFLUENCE_WEIGHTS["ema_trend"]))
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
    vol_filter_ok, _ = passes_volatility_filter(df_entry)
    sr_zones = find_sr_zones(df_macro)

    return {
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
        "vol_filter_ok": vol_filter_ok,
        "macd_bias": get_macd_bias(df_entry),
        "candle_pattern": detect_candlestick_pattern(df_entry),
        "divergence": detect_divergence(df_entry),
        "near_zone": nearest_sr_zone(sr_zones, close_price),
    }

def evaluate_signals(a):
    close_price, rsi_val, atr_val = a["close_price"], a["rsi_val"], a["atr_val"]
    trend_bullish, macro_bullish = a["trend_bullish"], a["macro_bullish"]
    buy_th, sell_th = ALERT_STATE["rsi_buy_threshold"], ALERT_STATE["rsi_sell_threshold"]
    min_score = ALERT_STATE["min_confluence_score"]

    signals_found, watch_found = [], []

    # Dynamic ATR Distance Calculations
    sl_dist = round(atr_val * ALERT_STATE["sl_atr_mult"], 2)
    tp1_dist = round(atr_val * ALERT_STATE["tp1_atr_mult"], 2)
    tp2_dist = round(atr_val * ALERT_STATE["tp2_atr_mult"], 2)

    if rsi_val <= buy_th and (trend_bullish or macro_bullish):
        sr_confluence = bool(a["near_zone"] and a["near_zone"]["type"] in ("support", "mixed") and close_price >= a["near_zone"]["price"])
        score, breakdown = compute_confluence_score("BUY", rsi_val, a["structure"], a["sweeps"], a["fvg"], a["vol_filter_ok"], a["macd_bias"], a["candle_pattern"], a["divergence"], sr_confluence, macro_bullish)

        if score >= min_score and ALERT_STATE["last_rsi_signal"] != "BUY":
            ALERT_STATE["last_rsi_signal"] = "BUY"
            sl_price, tp1_price, tp2_price = round(close_price - sl_dist, 2), round(close_price + tp1_dist, 2), round(close_price + tp2_dist, 2)
            log_signal_to_db("XAUUSD", "BUY", close_price, sl_price, tp1_price, tp2_price, score)

            msg = (
                f"🏆 **TRADING BUTLER SIGNAL ALERT** 🏆\n\n"
                f"• **Type:** `BUY 🟢` | **Timeframe:** {a['tf_label']}\n"
                f"📍 **Entry:** `${close_price}` | 📊 **14-ATR:** `${atr_val}`\n"
                f"🛡️ **Dynamic SL:** `${sl_price}` ({int(sl_dist*10)} pips)\n"
                f"🎯 **TP1:** `${tp1_price}` | 🎯 **TP2:** `${tp2_price}`\n\n"
                f"{format_confluence_breakdown(score, breakdown)}\n\n"
                f"💧 **Sweep:** {'Bullish Sweep ✅' if a['sweeps']['bullish_sweep'] else 'None'}\n"
                f"⚡ **FVG:** {'Bullish FVG ✅' if a['fvg']['bullish_fvg'] else 'None'}"
            )
            signals_found.append(msg)

    elif rsi_val >= sell_th and ((not trend_bullish) or (not macro_bullish)):
        sr_confluence = bool(a["near_zone"] and a["near_zone"]["type"] in ("resistance", "mixed") and close_price <= a["near_zone"]["price"])
        score, breakdown = compute_confluence_score("SELL", rsi_val, a["structure"], a["sweeps"], a["fvg"], a["vol_filter_ok"], a["macd_bias"], a["candle_pattern"], a["divergence"], sr_confluence, not macro_bullish)

        if score >= min_score and ALERT_STATE["last_rsi_signal"] != "SELL":
            ALERT_STATE["last_rsi_signal"] = "SELL"
            sl_price, tp1_price, tp2_price = round(close_price + sl_dist, 2), round(close_price - tp1_dist, 2), round(close_price - tp2_dist, 2)
            log_signal_to_db("XAUUSD", "SELL", close_price, sl_price, tp1_price, tp2_price, score)

            msg = (
                f"🏆 **TRADING BUTLER SIGNAL ALERT** 🏆\n\n"
                f"• **Type:** `SELL 🔴` | **Timeframe:** {a['tf_label']}\n"
                f"📍 **Entry:** `${close_price}` | 📊 **14-ATR:** `${atr_val}`\n"
                f"🛡️ **Dynamic SL:** `${sl_price}` ({int(sl_dist*10)} pips)\n"
                f"🎯 **TP1:** `${tp1_price}` | 🎯 **TP2:** `${tp2_price}`\n\n"
                f"{format_confluence_breakdown(score, breakdown)}\n\n"
                f"💧 **Sweep:** {'Bearish Sweep ✅' if a['sweeps']['bearish_sweep'] else 'None'}\n"
                f"⚡ **FVG:** {'Bearish FVG ✅' if a['fvg']['bearish_fvg'] else 'None'}"
            )
            signals_found.append(msg)

    elif (buy_th + ALERT_STATE["watch_rsi_margin"]) < rsi_val < (sell_th - ALERT_STATE["watch_rsi_margin"]):
        ALERT_STATE["last_rsi_signal"] = None

    return signals_found, watch_found