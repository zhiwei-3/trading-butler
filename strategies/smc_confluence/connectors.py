"""CONNECTORS: backtest hookup (live needs nothing beyond the core's analysis dict)."""


def prepare(bt, p):
    return {"rsi": bt.entry["RSI"].to_numpy(float)}


def bar_context(prep, i, p, state):
    """Skip bars whose RSI is nowhere near a trigger (threshold ±5) before any analysis is computed."""
    rsi = prep["rsi"][i]
    if not (rsi <= p["rsi_buy"] + 5 or rsi >= p["rsi_sell"] - 5):
        state["last"] = None
        return None
    return {}