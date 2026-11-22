# analysis/indicators.py
import numpy as np
import pandas as pd
import pandas_ta as ta
from .smc import find_swing_points

ATR_AVG_PERIOD = 20
VOLUME_AVG_PERIOD = 20


def _num(series, index):
    """pandas_ta returns None when there are too few bars; turn that into an all-NaN column."""
    if series is None:
        return pd.Series(np.nan, index=index)
    return pd.to_numeric(series, errors='coerce')


def add_indicators(df, extras=False):
    """Adds EMA_20/EMA_50/RSI/ATR. extras=True (entry timeframe) also adds ATR_AVG, VOL_AVG, MACD_BIAS.
    The ONLY place indicator columns are built — live fetches and backtest history both go through here."""
    df = df.copy()
    df['EMA_20'] = _num(ta.ema(df['close'], length=20), df.index)
    df['EMA_50'] = _num(ta.ema(df['close'], length=50), df.index)
    df['RSI'] = _num(ta.rsi(df['close'], length=14), df.index)
    df['ATR'] = _num(ta.atr(df['high'], df['low'], df['close'], length=14), df.index)
    if not extras:
        return df

    df['ATR_AVG'] = df['ATR'].rolling(ATR_AVG_PERIOD).mean()
    df['VOL_AVG'] = df['tick_volume'].rolling(VOLUME_AVG_PERIOD).mean() if 'tick_volume' in df.columns else np.nan

    df['MACD_BIAS'] = None
    macd_df = ta.macd(df['close'], fast=12, slow=26, signal=9)
    if macd_df is not None and not macd_df.empty:
        macd_col = next((c for c in macd_df.columns if c.startswith("MACD_")), None)
        signal_col = next((c for c in macd_df.columns if c.startswith("MACDs_")), None)
        if macd_col and signal_col:
            df['MACD_BIAS'] = np.where(macd_df[macd_col] > macd_df[signal_col], "BULLISH", "BEARISH")
    return df


def vol_filter_ok(row, atr_multiplier=1.0, volume_multiplier=1.2):
    """Row-based volatility/volume gate (needs the extras columns)."""
    atr_ok = True
    if pd.notna(row.get('ATR_AVG')) and pd.notna(row.get('ATR')):
        atr_ok = bool(row['ATR'] >= row['ATR_AVG'] * atr_multiplier)
    vol_ok = True
    if 'tick_volume' in row.index and pd.notna(row.get('VOL_AVG')) and pd.notna(row['tick_volume']):
        vol_ok = bool(row['tick_volume'] >= row['VOL_AVG'] * volume_multiplier)
    return atr_ok and vol_ok


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

    out = []
    for z in zones:
        if z["touches"] < min_touches:
            continue
        center = z["price"]
        half_min = center * cluster_pct * 0.5          # minimum box half-thickness
        low = min(min(z["prices"]), center - half_min)
        high = max(max(z["prices"]), center + half_min)
        out.append({
            "price": round(center, 2),
            "low": round(low, 2),
            "high": round(high, 2),
            "touches": z["touches"],
            "type": "mixed" if len(z["types"]) > 1 else next(iter(z["types"])),
        })
    return sorted(out, key=lambda z: z["touches"], reverse=True)


def nearest_sr_zone(zones, price, max_distance_pct=0.004):
    candidates = [z for z in zones if abs(z["price"] - price) / price <= max_distance_pct]
    return sorted(candidates, key=lambda z: abs(z["price"] - price))[0] if candidates else None