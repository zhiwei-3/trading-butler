# utils/mtf.py
"""Backtest helper for strategies that read a higher timeframe: no look-ahead."""
import numpy as np


class ClosedBarCursor:
    """Forward-only cursor over a higher-TF frame. at(t) = index of the last bar that is fully
    CLOSED at time t (-1 if none). Call with non-decreasing t, e.g. entry-bar open + entry TF length."""
    def __init__(self, times, tf_minutes):
        self.close = np.asarray(times, dtype="datetime64[ns]") + np.timedelta64(int(tf_minutes), "m")
        self.idx = -1

    def at(self, t):
        n = len(self.close)
        while self.idx + 1 < n and self.close[self.idx + 1] <= t:
            self.idx += 1
        return self.idx