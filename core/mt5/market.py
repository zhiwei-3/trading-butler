# core/mt5/market.py
"""Market data. Timeframes are plain names ("M5", "H1" — see config.TF_MINUTES); the MT5 constant is
resolved here and nowhere else."""

import logging

import pandas as pd

from config import TF_MINUTES
from core.mt5.gateway import mt5


def tf_const(name):
    """'M5' -> mt5.TIMEFRAME_M5."""
    if name not in TF_MINUTES:
        raise ValueError(f"Unknown timeframe '{name}' (valid: {', '.join(TF_MINUTES)})")
    return getattr(mt5, f"TIMEFRAME_{name}")


def fetch_candles(symbol, timeframe, count=100):
    """The latest `count` candles, newest last. Returns None when MT5 has nothing."""
    rates = mt5.copy_rates_from_pos(symbol, tf_const(timeframe), 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    # tz-AWARE UTC, matching fetch_history: the live and backtest paths must agree on what a bar's timestamp means.
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    return df


def fetch_history(symbol, timeframe, start, end):
    """Candles in [start, end] (broker-server time). Returns None when MT5 has nothing."""
    rates = mt5.copy_rates_range(symbol, tf_const(timeframe), start, end)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True).astype('datetime64[ns, UTC]')
    return df


def get_tick(symbol):
    """Locked wrapper around symbol_info_tick."""
    if not symbol:
        logging.warning("get_tick called with None/empty symbol")
        return None
    return mt5.symbol_info_tick(symbol)