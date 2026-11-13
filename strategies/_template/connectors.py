# strategies/_template/connectors.py
"""CONNECTORS: turn the data the core provides into the ctx the rules consume.
Live and backtest produce the SAME ctx shape, so rules.py never knows which one it is in."""

TAIL_PAD = 5


def live_context(md, p):
    """md.entry's last row is the still-forming bar; the rules want CLOSED bars only."""
    n = int(p["lookback"])
    df = md.entry
    if df is None or len(df) < n + TAIL_PAD + 1:
        return None
    closed = df.iloc[:-1].tail(n + TAIL_PAD)
    return {
        "h": closed["high"].to_numpy(float),
        "l": closed["low"].to_numpy(float),
        "c": closed["close"].to_numpy(float),
        "bar_id": str(closed["time"].iloc[-1]),
    }


def prepare(bt, p):
    """Once per backtest run."""
    e = bt.entry
    return {"h": e["high"].to_numpy(float), "l": e["low"].to_numpy(float),
            "c": e["close"].to_numpy(float), "t": e["time"].to_numpy()}


def bar_context(prep, i, p, state):
    """Once per bar. Return None to skip the bar (not enough history)."""
    lo_i = i - (int(p["lookback"]) + TAIL_PAD) + 1
    if lo_i < 0:
        return None
    return {"h": prep["h"][lo_i:i + 1], "l": prep["l"][lo_i:i + 1], "c": prep["c"][lo_i:i + 1],
            "bar_id": str(prep["t"][i])}