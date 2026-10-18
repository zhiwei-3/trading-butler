# strategy/smc.py

import numpy as np

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
        return {"bullish_ob": False, "bearish_ob": False,
                "bullish_ob_level": None, "bearish_ob_level": None,
                "ob_level": None}

    recent = df.iloc[-lookback:].reset_index(drop=True)
    c_curr = recent.iloc[-1]
    bullish_ob = bearish_ob = False
    bull_level = bear_level = None

    # Bullish OB: Last down-candle before strong bullish displacement
    for i in range(len(recent) - 4, 1, -1):
        c_ob = recent.iloc[i]
        c_exp = recent.iloc[i + 1]
        if c_ob['close'] < c_ob['open'] and (c_exp['close'] - c_exp['open']) > 1.5 * abs(c_ob['open'] - c_ob['close']):
            if c_curr['low'] <= c_ob['high'] and c_curr['close'] >= c_ob['low']:
                bullish_ob = True
                bull_level = round(c_ob['low'], 2)
                break

    # Bearish OB: Last up-candle before strong bearish drop
    for i in range(len(recent) - 4, 1, -1):
        c_ob = recent.iloc[i]
        c_exp = recent.iloc[i + 1]
        if c_ob['close'] > c_ob['open'] and (c_exp['open'] - c_exp['close']) > 1.5 * abs(c_ob['open'] - c_ob['close']):
            if c_curr['high'] >= c_ob['low'] and c_curr['close'] <= c_ob['high']:
                bearish_ob = True
                bear_level = round(c_ob['high'], 2)
                break

    return {"bullish_ob": bullish_ob, "bearish_ob": bearish_ob,
            "bullish_ob_level": bull_level, "bearish_ob_level": bear_level,
            "ob_level": bull_level if bullish_ob else bear_level}


# ---------------------------------------------------------------- IFVG (liquidity sweep + inverted FVG)

_IFVG_FVG_LEAD_BARS = 12   # the bearish FVG must form within this many bars before the sweep bar


def _ifvg_bullish_core(o, h, l, c, tol, left, right, max_setup_bars, min_body):
    """
    Bullish-orientation IFVG scan on raw numpy arrays (last element = current bar).
    The SELL side is handled by the caller mirroring prices (negating and swapping
    highs/lows), so this logic is written once.
    """
    n = len(c)
    if n < 20:
        return None

    swing_lows = [(i, l[i]) for i in range(left, n - right)
                  if l[i] == l[i - left:i + right + 1].min()]
    if len(swing_lows) < 2:
        return None

    # 1. Equal-low pools (SSL), newest first
    pools = []
    for b in range(len(swing_lows) - 1, 0, -1):
        jb, pb = swing_lows[b]
        for a in range(b - 1, -1, -1):
            ja, pa = swing_lows[a]
            if jb - ja >= 3 and abs(pa - pb) <= tol:
                pools.append((jb, min(pa, pb)))
                break

    for jb, level in pools:
        # 2. Liquidity sweep: first bar after the pool that trades below it
        below = np.nonzero(l[jb + 1:] < level)[0]
        if below.size == 0:
            continue
        s = jb + 1 + int(below[0])
        if (n - 1) - s > max_setup_bars:
            continue

        # 3. Bearish FVG from the down-move, inverted by a bullish close above it
        for g in range(n - 3, max(jb, 1), -1):
            if g < s - _IFVG_FVG_LEAD_BARS:
                break
            if not h[g] < l[g - 2]:
                continue
            top, bottom = float(l[g - 2]), float(h[g])

            above = np.nonzero(c[g + 1:] > top)[0]
            if above.size == 0:
                continue
            m = g + 1 + int(above[0])            # inversion bar
            if m < s or m > n - 2:               # must be at/after the sweep, with a bar left to retest
                continue
            body = c[m] - o[m]
            if body <= 0 or body < min_body:     # needs real displacement
                continue
            if (c[m + 1:] < bottom).any():       # zone failed (closed through it)
                continue
            if m + 1 < n - 1 and (l[m + 1:n - 1] <= top).any():
                continue                         # zone already retested earlier
            # 4. Retest on the current bar
            if l[n - 1] > top:
                continue

            return {
                "level": float(level),
                "sweep_extreme": float(l[s:m + 1].min()),
                "zone_top": top, "zone_bottom": bottom,
                "pool_idx": jb, "sweep_idx": s, "fvg_idx": g, "inv_idx": m,
            }
    return None


def detect_ifvg_setup(df, direction, atr_val, lookback=100, left=2, right=2,
                      eq_tolerance_atr=0.3, max_setup_bars=40, min_body_atr=0.4):
    """
    Liquidity Sweep -> Displacement -> IFVG retest.
    BUY : equal lows swept, bearish FVG inverted (closed above), price retests the gap from above.
    SELL: mirror image.
    Returns None, or a dict with zone_top/zone_bottom (also exposed as fvg_top/fvg_bottom
    for the chart), level, sweep_extreme and a dedup "key".
    """
    if df is None or len(df) < 30 or not atr_val or atr_val <= 0:
        return None

    w = df.tail(lookback)
    o, h, l, c = (w[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    if direction == "SELL":
        o, h, l, c = -o, -l, -h, -c

    res = _ifvg_bullish_core(o, h, l, c, eq_tolerance_atr * atr_val, left, right,
                             max_setup_bars, min_body_atr * atr_val)
    if res is None:
        return None

    if direction == "SELL":
        res["zone_top"], res["zone_bottom"] = -res["zone_bottom"], -res["zone_top"]
        res["level"] = -res["level"]
        res["sweep_extreme"] = -res["sweep_extreme"]

    for k in ("zone_top", "zone_bottom", "level", "sweep_extreme"):
        res[k] = round(res[k], 2)
    res["fvg_top"], res["fvg_bottom"] = res["zone_top"], res["zone_bottom"]

    inv_time = w["time"].iloc[res["inv_idx"]] if "time" in w.columns else res["inv_idx"]
    res["key"] = f"{direction}|{inv_time}|{res['zone_bottom']}|{res['zone_top']}"
    return res