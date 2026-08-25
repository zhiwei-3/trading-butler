import pandas as pd
import pandas_ta as ta
from strategy.smc import find_swing_points

def passes_volatility_filter(df, atr_multiplier=1.0, volume_multiplier=1.2, atr_period=14, avg_period=20):
    df = df.copy()
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=atr_period)
    avg_atr = df['ATR'].rolling(avg_period).mean().iloc[-1]
    latest_atr = df['ATR'].iloc[-1]
    atr_ok = bool(latest_atr >= avg_atr * atr_multiplier) if pd.notna(avg_atr) and pd.notna(latest_atr) else True

    vol_ok = True
    if 'tick_volume' in df.columns:
        avg_vol = df['tick_volume'].rolling(avg_period).mean().iloc[-1]
        latest_vol = df['tick_volume'].iloc[-1]
        if pd.notna(avg_vol) and pd.notna(latest_vol):
            vol_ok = bool(latest_vol >= avg_vol * volume_multiplier)

    return (atr_ok and vol_ok), {"latest_atr": round(latest_atr, 2) if pd.notna(latest_atr) else None}

def get_macd_bias(df, fast=12, slow=26, signal=9):
    macd_df = ta.macd(df['close'], fast=fast, slow=slow, signal=signal)
    if macd_df is None or macd_df.empty:
        return None
    macd_col = next((c for c in macd_df.columns if c.startswith("MACD_")), None)
    signal_col = next((c for c in macd_df.columns if c.startswith("MACDs_")), None)
    if not macd_col or not signal_col:
        return None
    line, sig = macd_df[macd_col].iloc[-1], macd_df[signal_col].iloc[-1]
    return "BULLISH" if line > sig else "BEARISH"

def detect_candlestick_pattern(df):
    if len(df) < 2:
        return None
    o1, h1, l1, c1 = df[['open', 'high', 'low', 'close']].iloc[-2]
    o2, h2, l2, c2 = df[['open', 'high', 'low', 'close']].iloc[-1]
    body2, range2 = abs(c2 - o2), h2 - l2
    if range2 <= 0:
        return None
    upper_wick, lower_wick = h2 - max(c2, o2), min(c2, o2) - l2

    if c1 < o1 and c2 > o2 and c2 >= o1 and o2 <= c1:
        return "BULLISH_ENGULFING"
    if c1 > o1 and c2 < o2 and o2 >= c1 and c2 <= o1:
        return "BEARISH_ENGULFING"
    if body2 > 0 and lower_wick >= 2 * body2 and upper_wick <= body2 * 0.5:
        return "HAMMER"
    if body2 > 0 and upper_wick >= 2 * body2 and lower_wick <= body2 * 0.5:
        return "SHOOTING_STAR"
    if body2 / range2 < 0.1:
        return "DOJI"
    return None

def detect_divergence(df, rsi_col='RSI', lookback=60, left=2, right=2):
    result = {"bullish": False, "bearish": False}
    recent = df.iloc[-lookback:].reset_index(drop=True)
    if rsi_col not in recent.columns:
        return result
    swing_highs, swing_lows = find_swing_points(recent, left, right)

    if len(swing_lows) >= 2:
        (i1, p1), (i2, p2) = swing_lows[-2], swing_lows[-1]
        r1, r2 = recent[rsi_col].iloc[i1], recent[rsi_col].iloc[i2]
        if pd.notna(r1) and pd.notna(r2) and p2 < p1 and r2 > r1:
            result["bullish"] = True
    if len(swing_highs) >= 2:
        (i1, p1), (i2, p2) = swing_highs[-2], swing_highs[-1]
        r1, r2 = recent[rsi_col].iloc[i1], recent[rsi_col].iloc[i2]
        if pd.notna(r1) and pd.notna(r2) and p2 > p1 and r2 < r1:
            result["bearish"] = True
    return result

def find_sr_zones(df, lookback=180, left=3, right=3, cluster_pct=0.0015, min_touches=2):
    recent = df.iloc[-lookback:].reset_index(drop=True)
    swing_highs, swing_lows = find_swing_points(recent, left, right)
    points = sorted([(p, "resistance") for _, p in swing_highs] + [(p, "support") for _, p in swing_lows], key=lambda x: x[0])
    zones = []
    for price, kind in points:
        merged = False
        for zone in zones:
            if abs(price - zone["price"]) / zone["price"] <= cluster_pct:
                zone["prices"].append(price)
                zone["price"] = sum(zone["prices"]) / len(zone["prices"])
                zone["touches"] += 1
                zone["types"].add(kind)
                merged = True
                break
        if not merged:
            zones.append({"price": price, "prices": [price], "touches": 1, "types": {kind}})
    return sorted([
        {"price": round(z["price"], 2), "touches": z["touches"], "type": ("mixed" if len(z["types"]) > 1 else next(iter(z["types"])))}
        for z in zones if z["touches"] >= min_touches
    ], key=lambda z: z["touches"], reverse=True)

def nearest_sr_zone(zones, price, max_distance_pct=0.004):
    candidates = [z for z in zones if abs(z["price"] - price) / price <= max_distance_pct]
    return sorted(candidates, key=lambda z: abs(z["price"] - price))[0] if candidates else None