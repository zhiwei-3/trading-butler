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
    if df is None or len(df) < 3:
        return None
    
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
    """
    Detects a 3-candle Fair Value Gap at the END of the window (i.e. the most
    recent gap only). Used for the LTF/entry-timeframe check, where "did a gap
    just form" is exactly what's wanted.

    BUGFIX: previously returned only bullish_fvg/bearish_fvg/gap_size, but three
    call sites (the htf_fvg_sweep strategy's price-containment check, and the
    signal chart's FVG shading) read fvg_top/fvg_bottom that were never present
    — so the containment check silently degraded to "always true" and the chart
    never drew the box. Both fields are now always returned (None when no gap).
    """
    empty = {"bullish_fvg": False, "bearish_fvg": False, "gap_size": 0.0,
             "fvg_top": None, "fvg_bottom": None}
    if len(df) < 3:
        return empty
    c1, c3 = df.iloc[-3], df.iloc[-1]
    if c3['low'] > c1['high']:
        return {"bullish_fvg": True, "bearish_fvg": False,
                "gap_size": round(c3['low'] - c1['high'], 2),
                "fvg_bottom": round(float(c1['high']), 2), "fvg_top": round(float(c3['low']), 2)}
    if c3['high'] < c1['low']:
        return {"bullish_fvg": False, "bearish_fvg": True,
                "gap_size": round(c1['low'] - c3['high'], 2),
                "fvg_bottom": round(float(c3['high']), 2), "fvg_top": round(float(c1['low']), 2)}
    return empty

def find_unmitigated_fvgs(df, lookback=100, max_gaps=5):
    """
    Scans a window for 3-candle Fair Value Gaps and returns the ones that
    haven't yet been filled by later price action (i.e. still "live" zones).

    BUGFIX: the HTF/macro FVG tap used detect_fvg(df_macro), which only ever
    inspects the LAST 3 macro bars — so a macro gap was visible to the strategy
    for exactly one bar and there was no way to detect price returning to tap an
    older gap later, which is the entire premise of "HTF FVG tap" strategies.
    """
    if len(df) < 3:
        return []
    recent = df.iloc[-lookback:].reset_index(drop=True)
    n = len(recent)
    gaps = []
    for i in range(2, n):
        c1 = recent.iloc[i - 2]
        c3 = recent.iloc[i]
        if c3['low'] > c1['high']:
            gaps.append({"type": "bullish", "top": float(c3['low']), "bottom": float(c1['high']), "formed_at": i})
        elif c3['high'] < c1['low']:
            gaps.append({"type": "bearish", "top": float(c1['low']), "bottom": float(c3['high']), "formed_at": i})

    unmitigated = []
    for gap in gaps:
        filled = False
        for j in range(gap["formed_at"] + 1, n):
            low_j, high_j = recent['low'].iloc[j], recent['high'].iloc[j]
            if gap["type"] == "bullish" and low_j <= gap["bottom"]:
                filled = True
                break
            if gap["type"] == "bearish" and high_j >= gap["top"]:
                filled = True
                break
        if not filled:
            unmitigated.append(gap)

    return unmitigated[-max_gaps:]

def fvg_snapshot_from_gaps(gaps, price):
    """Picks the unmitigated gap price is currently sitting inside (if any) and
    formats it into the bullish_fvg/bearish_fvg/fvg_top/fvg_bottom shape the
    strategies expect."""
    inside = [g for g in gaps if g["bottom"] <= price <= g["top"]]
    hit = inside[-1] if inside else None
    if hit is None:
        return {"bullish_fvg": False, "bearish_fvg": False, "fvg_top": None, "fvg_bottom": None}
    return {
        "bullish_fvg": hit["type"] == "bullish",
        "bearish_fvg": hit["type"] == "bearish",
        "fvg_top": round(hit["top"], 2),
        "fvg_bottom": round(hit["bottom"], 2),
    }

def macro_fvg_snapshot(df, price, lookback=100, max_gaps=5):
    """Convenience wrapper: find unmitigated HTF gaps and report whether the
    current price is tapping one of them right now."""
    return fvg_snapshot_from_gaps(find_unmitigated_fvgs(df, lookback, max_gaps), price)

def detect_order_block(df, lookback=20):
    """Detects recent Bullish/Bearish Order Blocks (OB) and mitigation zones."""
    if len(df) < lookback:
        return {"bullish_ob": False, "bearish_ob": False, "ob_level": None}

    recent = df.iloc[-lookback:].reset_index(drop=True)
    c_curr = recent.iloc[-1]
    bullish_ob, bearish_ob, ob_level = False, False, None

    # Bullish OB: Last down-candle before strong bullish displacement
    for i in range(len(recent) - 4, 1, -1):
        c_ob = recent.iloc[i]
        c_exp = recent.iloc[i + 1]
        if c_ob['close'] < c_ob['open'] and (c_exp['close'] - c_exp['open']) > 1.5 * abs(c_ob['open'] - c_ob['close']):
            if c_curr['low'] <= c_ob['high'] and c_curr['close'] >= c_ob['low']:
                bullish_ob = True
                ob_level = round(c_ob['low'], 2)
                break

    # Bearish OB: Last up-candle before strong bearish drop
    for i in range(len(recent) - 4, 1, -1):
        c_ob = recent.iloc[i]
        c_exp = recent.iloc[i + 1]
        if c_ob['close'] > c_ob['open'] and (c_exp['open'] - c_exp['close']) > 1.5 * abs(c_ob['open'] - c_ob['close']):
            if c_curr['high'] >= c_ob['low'] and c_curr['close'] <= c_ob['high']:
                bearish_ob = True
                ob_level = round(c_ob['high'], 2)
                break

    return {"bullish_ob": bullish_ob, "bearish_ob": bearish_ob, "ob_level": ob_level}