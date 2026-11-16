# tests/test_strategies.py
import numpy as np
import pandas as pd
import pytest

from analysis import ALL, View, add_indicators, compute, default_config
from strategies import STRATEGIES, MarketData, Params, get_params

pytestmark = pytest.mark.skipif(not STRATEGIES, reason="no strategies installed")


def _frame(n=420, seed=1, freq="5min"):
    rng = np.random.default_rng(seed)
    close = 2000 + np.cumsum(rng.normal(0, 1.5, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0.1, 1.5, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 1.5, n)
    return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC"),
                         "open": open_, "high": high, "low": low, "close": close,
                         "tick_volume": rng.integers(50, 500, n)})


def _analysis(strat):
    entry = add_indicators(_frame(), extras=True)
    trend, macro = add_indicators(_frame(seed=2)), add_indicators(_frame(seed=3))
    price, atr = round(entry["close"].iloc[-1], 2), round(entry["ATR"].iloc[-1], 2)
    view = View(entry=entry, price=price, atr=atr, cfg=default_config(), macro=macro)
    a = {"symbol": "XAUUSD", "df_entry": entry, "tf_label": "test", "close_price": price,
         "rsi_val": round(entry["RSI"].iloc[-1], 2), "atr_val": atr,
         "entry_bullish": True, "trend_bullish": True, "macro_bullish": False,
         **compute(ALL, view), "ctx": None}
    extra = {name: _frame(seed=4, freq="1h") for name, _ in strat.needs.extra}
    md = MarketData(symbol="XAUUSD", entry=entry, trend=trend, macro=macro, extra=extra, broker_offset_s=0)
    a["ctx"] = strat.build_live_context(md, get_params(strat.key))
    return a


@pytest.mark.parametrize("key", list(STRATEGIES))
def test_defaults_parse_and_are_valid(key):
    strat = STRATEGIES[key]
    for prm in strat.params:
        assert prm.parse(str(prm.default)) == prm.default, f"{key}.{prm.key} default does not round-trip"
        for v in prm.sweep:
            prm.parse(str(v))                               # every sweep value must be a legal value
    assert strat.valid_params(Params({p.key: p.default for p in strat.params}))


@pytest.mark.parametrize("key", list(STRATEGIES))
def test_live_hooks_do_not_raise(key):
    strat = STRATEGIES[key]
    a, p = _analysis(strat), get_params(key)
    assert isinstance(strat.evaluate(a, p, {}), list)
    assert isinstance(strat.watch(a, p, {}), list)
    assert isinstance(strat.summary(p), str)
    assert isinstance(strat.diagnose(a, p), str)


@pytest.mark.parametrize("key", list(STRATEGIES))
def test_features_declared_are_the_ones_used(key):
    """A strategy reading a feature it didn't declare would KeyError on live. Compute ONLY the declared set."""
    strat = STRATEGIES[key]
    entry = add_indicators(_frame(), extras=True)
    macro = add_indicators(_frame(seed=3))
    price, atr = round(entry["close"].iloc[-1], 2), round(entry["ATR"].iloc[-1], 2)
    view = View(entry=entry, price=price, atr=atr, cfg=default_config(), macro=macro)
    a = {"symbol": "XAUUSD", "df_entry": entry, "tf_label": "test", "close_price": price,
         "rsi_val": round(entry["RSI"].iloc[-1], 2), "atr_val": atr,
         "entry_bullish": True, "trend_bullish": True, "macro_bullish": False,
         **compute(strat.needs.features, view), "ctx": None}
    strat.evaluate(a, get_params(key), {})                  # KeyError here = an undeclared feature