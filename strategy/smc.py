def find_swing_points(df, left=2, right=2):
    """Identifies fractal swing highs/lows."""
    highs, lows = df['high'].values, df['low'].values
    n = len(df)
    swing_highs, swing_lows = [], []
    for i in range(left, n - right):
        if highs[i] == highs[i - left:i + right + 1].max():
            swing_highs.append((i, highs[i]))
        if lows[i] == lows[i - left:i + right + 1].min():
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows

def detect_market_structure(df, lookback=60, left=2, right=2):
    """Detects fresh SMC Break-of-Structure (BOS)."""
    if len(df) < lookback:
        lookback = len(df)
    recent_df = df.iloc[-lookback:].reset_index(drop=True)

    swing_highs, swing_lows = find_swing_points(recent_df, left, right)
    if not swing_highs or not swing_lows:
        return None

    last_swing_high = swing_highs[-1][1]
    last_swing_low = swing_lows[-1][1]
    
    # Verify the break occurred on the current or previous candle while earlier candles stayed inside range
    c_curr = recent_df['close'].iloc[-1]
    c_prev = recent_df['close'].iloc[-2]
    c_prior = recent_df['close'].iloc[-3]

    if c_curr > last_swing_high and c_prior <= last_swing_high:
        return "BULLISH_BOS"
    if c_curr < last_swing_low and c_prior >= last_swing_low:
        return "BEARISH_BOS"
        
    return None

def detect_liquidity_sweeps(df, swing_highs, swing_lows):
    """Detects wick sweeps past recent swing extremes."""
    if len(df) < 2 or not swing_highs or not swing_lows:
        return {"bullish_sweep": False, "bearish_sweep": False}
    latest = df.iloc[-1]
    last_high = swing_highs[-1][1]
    last_low = swing_lows[-1][1]

    bullish_sweep = bool(latest['low'] < last_low and latest['close'] > last_low)
    bearish_sweep = bool(latest['high'] > last_high and latest['close'] < last_high)
    return {"bullish_sweep": bullish_sweep, "bearish_sweep": bearish_sweep}

def detect_fvg(df):
    """Detects 3-candle Fair Value Gaps."""
    if len(df) < 3:
        return {"bullish_fvg": False, "bearish_fvg": False, "gap_size": 0.0}
    c1, c3 = df.iloc[-3], df.iloc[-1]
    bullish_fvg = bool(c3['low'] > c1['high'])
    bearish_fvg = bool(c3['high'] < c1['low'])
    gap_size = round(c3['low'] - c1['high'], 2) if bullish_fvg else (round(c1['low'] - c3['high'], 2) if bearish_fvg else 0.0)
    return {"bullish_fvg": bullish_fvg, "bearish_fvg": bearish_fvg, "gap_size": gap_size}