import numpy as np
import pandas as pd
import pytest

from strategies import STRATEGIES, MarketData, Params
from utils.mtf import ClosedBarCursor

pytestmark = pytest.mark.skipif("trend_breakout" not in STRATEGIES, reason="trend_breakout not installed")

TB = STRATEGIES.get("trend_breakout")
P = Params({prm.key: prm.default for prm in TB.params}) if TB else None


def _rules():
    from strategies.trend_breakout import rules
    return rules


def _ctx(c, ah=2010.0, al=2000.0, trend="UP", hour=8.5):
    return {"trend": trend, "ah": ah, "al": al, "hour": hour, "day_key": "2026-01-05", "bar_minutes": 5,
            "h": c + 1.0, "l": c - 1.0, "c": c}


def test_cursor_has_no_lookahead():
    times = pd.date_range("2026-01-05 00:00", periods=5, freq="1h", tz="UTC").values      # H1 bars open 00:00..04:00
    cur = ClosedBarCursor(times, 60)
    assert cur.at(np.datetime64("2026-01-05T00:59:00", "ns")) == -1     # first bar closes 01:00
    assert cur.at(np.datetime64("2026-01-05T01:00:00", "ns")) == 0
    assert cur.at(np.datetime64("2026-01-05T03:30:00", "ns")) == 2


def test_breakout_buy_fires_once():
    c = np.full(20, 2005.0)
    c[-2], c[-1] = 2009.0, 2011.0                       # fresh close above the Asian high (2010)
    found = _rules().find_trend_breakout(_ctx(c), 2011.0, 3.0, 1.5, P, set())
    assert found and found["direction"] == "BUY" and found["setup"]["mode"] == "BREAKOUT"
    t = found["targets"]
    assert t["sl_price"] < 2011.0 < t["tp1_price"] < t["tp2_price"]
    assert _rules().find_trend_breakout(_ctx(c), 2011.0, 3.0, 1.5, P, {found["setup"]["key"]}) is None


def test_breakout_sell_mirrors_buy():
    c = np.full(20, 2005.0)
    c[-2], c[-1] = 2001.0, 1999.0                       # fresh close below the Asian low (2000)
    found = _rules().find_trend_breakout(_ctx(c, trend="DOWN"), 1999.0, 3.0, 1.5, P, set())
    assert found and found["direction"] == "SELL"
    t = found["targets"]
    assert t["tp2_price"] < t["tp1_price"] < 1999.0 < t["sl_price"]


def test_outside_session_or_chop_is_ignored():
    c = np.full(20, 2005.0)
    c[-2], c[-1] = 2009.0, 2011.0
    assert _rules().find_trend_breakout(_ctx(c, hour=11.0), 2011.0, 3.0, 1.5, P, set()) is None
    assert _rules().find_trend_breakout(_ctx(c, trend=None), 2011.0, 3.0, 1.5, P, set()) is None


def _walk(index, seed):
    rng = np.random.default_rng(seed)
    close = 2000 + np.cumsum(rng.normal(0, 1.5, len(index)))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"time": index, "open": open_,
                         "high": np.maximum(open_, close) + rng.uniform(0.1, 1.5, len(index)),
                         "low": np.minimum(open_, close) - rng.uniform(0.1, 1.5, len(index)),
                         "close": close, "tick_volume": 100})


def test_live_and_backtest_connectors_build_the_same_context():
    from strategies.trend_breakout import connectors
    from strategies import BacktestData

    # Last (forming) entry bar opens 09:00 UTC, so the last CLOSED bar (08:55) is inside the London window.
    entry = _walk(pd.date_range(end="2026-01-05 09:00", periods=420, freq="5min", tz="UTC"), 1)
    h1 = _walk(pd.date_range(end="2026-01-05 09:00", periods=420, freq="1h", tz="UTC"), 2)
    p = Params({**P, "adx_min": 0.0})                    # make the regime non-chop so both paths return a context

    live = connectors.live_context(
        MarketData(symbol="XAUUSD", entry=entry, trend=None, macro=None, extra={"H1": h1}, broker_offset_s=0), p)

    bt = BacktestData(symbol="XAUUSD", entry=entry, trend=None, macro=None, extra={"H1": h1},
                      entry_minutes=5, broker_offset_s=0, memo={})
    prep = connectors.prepare(bt, p)
    back = connectors.bar_context(prep, len(entry) - 2, p, {})

    assert live is not None and back is not None
    assert live["trend"] == back["trend"]
    assert live["day_key"] == back["day_key"] and live["bar_minutes"] == back["bar_minutes"]
    assert np.isclose(live["hour"], back["hour"])
    assert np.isclose(live["ah"], back["ah"]) and np.isclose(live["al"], back["al"])
    for k in ("h", "l", "c"):
        assert np.array_equal(live[k], back[k])