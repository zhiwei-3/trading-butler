"""CONNECTORS: backtest hookup. (Live needs no extra data: the core's analysis dict already
carries rsi_val, near_zone and order_block.)"""


def prepare(bt, p):
    """Once per backtest run."""
    return {"rsi": bt.entry["RSI"].to_numpy(float)}


def bar_context(prep, i, p, state):
    """Cheap pre-gate: skip bars whose RSI is nowhere near a trigger, and clear the dedup memory
    while RSI is outside that band (same band the old backtester hardcoded: threshold ±5)."""
    rsi = prep["rsi"][i]
    if not (rsi <= p["rsi_buy"] + 5 or rsi >= p["rsi_sell"] - 5):
        state["last"] = None
        return None
    return {}