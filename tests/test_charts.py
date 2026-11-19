# tests/test_charts.py
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from charts import (generate_chart_snapshot, generate_signal_chart, generate_outcome_chart,
                    generate_journal_chart, generate_equity_chart)
from charts.timeutil import candle_index, series_epoch, utc_epoch

PNG = b"\x89PNG"


def _frame(n=120, seed=1):
    rng = np.random.default_rng(seed)
    close = 2000 + np.cumsum(rng.normal(0, 1.5, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0.1, 1.5, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 1.5, n)
    return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC"),
                         "open": open_, "high": high, "low": low, "close": close})


def _is_png(buf):
    return buf is not None and buf.read(4) == PNG


# ---------------------------------------------------------------- time mapping

def test_utc_epoch_treats_naive_values_as_utc():
    assert utc_epoch("2026-01-01 00:00:00") == utc_epoch(pd.Timestamp("2026-01-01", tz="UTC"))
    assert utc_epoch(None) is None and utc_epoch("garbage") is None


def test_candle_index_applies_the_broker_offset_and_never_clamps():
    ts = series_epoch(_frame()["time"])              # candles labelled in BROKER time, 5 min apart
    assert candle_index(ts, "2026-01-01 00:10:00", 7200) == 26      # 00:10 UTC is 02:10 broker = bar 26
    assert candle_index(ts, "2025-12-31 23:55:00", 0) is None       # before the window
    assert candle_index(ts, "2026-01-01 09:00:00", 0) is None       # after the last bar (window ends 08:20)
    assert candle_index(ts, None, 0) is None


# ---------------------------------------------------------------- every chart renders

def test_snapshot_renders_with_overlays_and_ignores_out_of_window_trades():
    df = _frame()
    price = float(df["close"].iloc[-1])
    trades = [
        {"id": 1, "direction": "BUY", "entry": price, "entry_time": df["time"].iloc[30], "status": "CLOSED_TP1",
         "tp1": price + 5, "closed_at": df["time"].iloc[50]},
        {"id": 2, "direction": "SELL", "entry": price, "entry_time": "2020-01-01 00:00:00", "status": "HIT_SL",
         "sl": price + 5, "closed_at": "2020-01-01 01:00:00"},               # far outside the window
    ]
    buf = generate_chart_snapshot(
        df, near_zone={"type": "support", "price": price - 3, "low": price - 4, "high": price - 2},
        order_block={"bullish_ob": True, "bullish_ob_top": price - 1, "bullish_ob_bottom": price - 2},
        macro_fvg={"fvg_top": price + 4, "fvg_bottom": price + 2}, trades=trades, server_offset_s=0)
    assert _is_png(buf)


@pytest.mark.parametrize("dual", [True, False])
def test_signal_chart_renders(dual):
    df = _frame()
    p = float(df["close"].iloc[-1])
    buf = generate_signal_chart(df, "XAUUSD", "BUY", p, p - 5, p + 7.5, p + 15, dual=dual,
                                fvg={"fvg_top": p + 3, "fvg_bottom": p + 1}, server_offset_s=0)
    assert _is_png(buf)


@pytest.mark.parametrize("status", ["HIT_SL", "CLOSED_TP1", "HIT_TP1", "HIT_TP2", "CLOSED_BE", "CLOSED_MANUAL"])
def test_outcome_chart_renders_for_every_status(status):
    df = _frame()
    p = float(df["close"].iloc[40])
    buf = generate_outcome_chart(df, p, p - 5, p + 7.5, p + 15, status, direction="BUY",
                                 entry_time=df["time"].iloc[40], exit_time=df["time"].iloc[70],
                                 dual=True, server_offset_s=0)
    assert _is_png(buf)


def test_journal_and_equity_charts_render():
    def trade(day, pnl, source):
        return {"time": datetime(2026, 1, day, 10, tzinfo=timezone.utc), "net_pnl": pnl, "source": source}
    trades = [trade(2, 5.0, "live"), trade(3, -3.0, "live"), trade(4, 2.0, "dry")]
    assert _is_png(generate_journal_chart(trades, "USD", "t", "s"))
    assert _is_png(generate_equity_chart([0.0, 1.5, 0.5, 2.0]))


def test_charts_return_none_when_there_is_nothing_to_draw():
    assert generate_chart_snapshot(_frame(5)) is None
    assert generate_equity_chart([0.0]) is None
    assert generate_journal_chart([{"time": datetime(2026, 1, 2, tzinfo=timezone.utc),
                                    "net_pnl": 1.0, "source": "live"}]) is None