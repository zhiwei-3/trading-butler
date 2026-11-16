# tests/test_analysis.py
import numpy as np
import pandas as pd
from analysis import ALL, FEATURES, View, add_indicators, compute, default_config


def _frame(n=320, seed=1):
    rng = np.random.default_rng(seed)
    close = 2000 + np.cumsum(rng.normal(0, 1.5, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0.1, 1.5, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 1.5, n)
    return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC"),
                         "open": open_, "high": high, "low": low, "close": close,
                         "tick_volume": rng.integers(50, 500, n)})


def test_every_feature_computes_and_is_lazy_consistent():
    entry = add_indicators(_frame(), extras=True)
    macro = add_indicators(_frame(seed=2))
    view = View(entry=entry, price=entry["close"].iloc[-1], atr=entry["ATR"].iloc[-1],
                cfg=default_config(), macro=macro)
    out = compute(ALL, view)
    assert set(out) == set(ALL)
    # asking again (or through a fresh view) must give identical results: no hidden state
    view2 = View(entry=lambda: entry, price=view.price, atr=view.atr, cfg=default_config(), macro=lambda: macro)
    assert repr(compute(ALL, view2)) == repr(out)


def test_macro_store_reuses_until_the_macro_key_changes():
    entry = add_indicators(_frame(), extras=True)
    macro = add_indicators(_frame(seed=2))
    store, calls = {}, []
    def lazy_macro():
        calls.append(1)
        return macro
    for key in (5, 5, 6):
        View(entry=entry, price=2000.0, atr=1.0, cfg=default_config(), macro=lazy_macro,
             macro_store=store, macro_key=key).get("sr_zones")
    assert len(calls) == 2          # computed for key 5 once, then again only for key 6