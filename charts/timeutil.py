# charts/timeutil.py
"""Time helpers. DB timestamps are TRUE UTC; MT5 candle times are BROKER-SERVER time (epoch labelled as UTC)."""

import numpy as np
import pandas as pd

_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")
_ONE_SEC = pd.Timedelta(seconds=1)


def utc_epoch(value):
    """Any timestamp-ish value -> UTC epoch seconds. Naive values are treated as UTC
    (that's how the DB stores them). Returns None if unparseable."""
    if value is None:
        return None
    try:
        t = pd.Timestamp(value)
        if pd.isna(t):
            return None
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        return int((t - _EPOCH) // _ONE_SEC)
    except Exception:
        return None


def series_epoch(times):
    """Candle 'time' column -> int64 epoch seconds. Resolution-agnostic (ns/us/ms/s)."""
    idx = pd.DatetimeIndex(pd.to_datetime(times, utc=True))
    return np.asarray((idx - _EPOCH) // _ONE_SEC, dtype="int64")


def resolve_offset(server_offset_s):
    """Broker-server UTC offset in seconds (add to a UTC epoch to get a server epoch).
    Callers supply it (core.mt5.get_broker_utc_offset_seconds); None means 'unknown' -> 0."""
    return int(server_offset_s) if server_offset_s is not None else 0


def candle_index(candle_ts, utc_value, offset_s):
    """
    Maps a true-UTC timestamp (from the DB) to the candle it occurred in, using
    floor-binning against BROKER-SERVER candle times. Returns None when the time
    lies outside the visible window — never clamps to an edge bar.
    """
    if candle_ts is None or len(candle_ts) == 0:
        return None
    t = utc_epoch(utc_value)
    if t is None:
        return None
    t += int(offset_s)
    bar = int(np.median(np.diff(candle_ts))) if len(candle_ts) > 1 else 60
    if t < candle_ts[0] or t >= candle_ts[-1] + max(bar, 1):
        return None
    return max(int(np.searchsorted(candle_ts, t, side="right")) - 1, 0)