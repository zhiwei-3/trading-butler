"""H1 trend regime. Pure."""

import numpy as np
import pandas as pd
import pandas_ta as ta

REGIME_EMA_FAST, REGIME_EMA_SLOW, REGIME_ADX_LEN = 50, 200, 14
MIN_H1_BARS = REGIME_EMA_SLOW + 30


def add_regime_columns(df):
    """Adds EMA_F (50), EMA_S (200) and ADX (14) to an H1 frame."""
    df = df.copy()
    df["EMA_F"] = pd.to_numeric(ta.ema(df["close"], length=REGIME_EMA_FAST), errors="coerce")
    df["EMA_S"] = pd.to_numeric(ta.ema(df["close"], length=REGIME_EMA_SLOW), errors="coerce")
    adx = ta.adx(df["high"], df["low"], df["close"], length=REGIME_ADX_LEN)
    col = f"ADX_{REGIME_ADX_LEN}"
    df["ADX"] = pd.to_numeric(adx[col], errors="coerce") if adx is not None and col in adx.columns else np.nan
    return df


def regime_trend(ema_f, ema_s, adx, adx_min):
    """'UP' | 'DOWN' | None (None = chop / not enough data)."""
    if not (np.isfinite(ema_f) and np.isfinite(ema_s) and np.isfinite(adx)):
        return None
    if adx <= adx_min:
        return None
    if ema_f > ema_s:
        return "UP"
    if ema_f < ema_s:
        return "DOWN"
    return None


def regime_codes(df, adx_min):
    """Vectorised regime for the backtester: +1 up, -1 down, 0 none."""
    ef, es, adx = (df[k].to_numpy(float) for k in ("EMA_F", "EMA_S", "ADX"))
    ok = np.isfinite(ef) & np.isfinite(es) & np.isfinite(adx) & (adx > adx_min)
    return np.where(ok & (ef > es), 1, np.where(ok & (ef < es), -1, 0))