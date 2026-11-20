# tests/test_db.py
import json

import pytest

import config
from config import ALERT_STATE
from core import db


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_FILE", str(tmp_path / "test.db"))
    db.init_db()
    return db


def _log(database, **kw):
    args = dict(symbol="XAUUSD", direction="BUY", entry_price=2000.0, sl_price=1990.0,
                tp1_price=2015.0, tp2_price=2030.0, score=70)
    args.update(kw)
    return database.log_signal_to_db(**args)


# ---------------------------------------------------------------- signals

def test_signal_lifecycle_is_tracked_until_it_closes(database):
    sid = _log(database, dual_entry=True, near_zone={"low": 1995.0, "high": 1998.0, "type": "support"})
    tracked = database.get_tracked_signals()
    assert [s["id"] for s in tracked] == [sid]
    assert (tracked[0]["status"], tracked[0]["dual_entry"], tracked[0]["zone_type"]) == ("PENDING", 1, "support")

    database.attach_trade_execution(sid, 1, 11, 0.01, "LIVE", 2000.1)
    database.attach_trade_execution(sid, 2, 12, 0.01, "LIVE", 2000.1)
    assert [s["id"] for s in database.get_active_live_signals()] == [sid]
    assert database.get_live_signal_links() == {11: {"id": sid, "leg": 1, "score": 70},
                                                12: {"id": sid, "leg": 2, "score": 70}}

    database.update_signal_statuses([("HIT_TP1", "2026-01-01 10:00:00", sid)])
    assert database.get_tracked_signals()[0]["status"] == "HIT_TP1"          # the runner is still tracked
    database.mark_leg2_be_moved(sid)
    assert database.get_tracked_signals()[0]["be_moved2"] == 1

    database.set_signal_status(sid, "HIT_TP2")
    assert database.get_tracked_signals() == []
    assert database.get_signal_stats() == {"HIT_TP2": 1}


def test_skipped_signals_never_reach_the_chart_the_journal_or_the_open_lists(database):
    skipped, kept = _log(database), _log(database, direction="SELL")
    database.attach_trade_execution(kept, 1, None, 0.01, "DRY", 2000.0)
    database.set_signal_status(skipped, "SKIPPED")

    assert [t["id"] for t in database.get_recent_trades_for_chart(10, "XAUUSD")] == [kept]
    assert [s["id"] for s in database.get_dry_run_journal_signals()] == [kept]
    assert [s["id"] for s in database.get_open_dry_run_signals()] == [kept]
    assert [s["id"] for s in database.get_open_executed_signals()] == [kept]
    assert database.get_open_executed_signals(exclude_id=kept) == []        # a signal never blocks itself


def test_live_fills_today_count_tickets_not_signals_and_ignore_dry_run(database):
    sid = _log(database, dual_entry=True)
    database.attach_trade_execution(sid, 1, 11, 0.01, "LIVE", 2000.0)
    database.attach_trade_execution(sid, 2, 12, 0.01, "LIVE", 2000.0)
    assert database.count_live_trades_today() == 2

    other = _log(database)
    database.attach_trade_execution(other, 1, None, 0.01, "DRY", 2000.0)
    assert database.count_live_trades_today() == 2


# ---------------------------------------------------------------- stats the circuit breaker and cooldown rely on

def test_daily_performance_uses_the_same_r_model_as_the_backtester(database):
    # entry 2000, SL 1990 (risk 10): TP1 = 1.5R, TP2 = 3R
    for status in ("CLOSED_TP1", "HIT_TP2", "CLOSED_BE", "HIT_SL"):
        database.set_signal_status(_log(database), status)
    st = database.get_daily_performance_stats()
    assert st["net_r"] == 3.5                                   # 1.5 + (1.5+3)/2 + 1.5/2 - 1
    assert (st["today_wins"], st["today_losses"], st["consecutive_losses"]) == (3, 1, 1)


def test_consecutive_losses_reset_on_a_win(database):
    for status in ("HIT_SL", "HIT_SL", "CLOSED_TP1", "HIT_SL"):
        database.set_signal_status(_log(database), status)
    st = database.get_daily_performance_stats()
    assert st["consecutive_losses"] == 1 and st["net_r"] == -1.5


def test_sl_cooldown_counts_down_from_the_newest_stop_loss(database, monkeypatch):
    monkeypatch.setitem(ALERT_STATE, "sl_cooldown_enabled", True)
    monkeypatch.setitem(ALERT_STATE, "sl_cooldown_minutes", 30)
    assert database.get_sl_cooldown_remaining_s() == 0.0

    database.set_signal_status(_log(database), "HIT_SL")
    assert 1700 < database.get_sl_cooldown_remaining_s() <= 1800

    monkeypatch.setitem(ALERT_STATE, "sl_cooldown_enabled", False)
    assert database.get_sl_cooldown_remaining_s() == 0.0


# ---------------------------------------------------------------- sweeps

def _row(strategy, net_r):
    return {"strategy": strategy, "params": {"min_rrr": 2.0, "sl_mult": 1.5, "rsi_buy": 30.0},
            "total_trades": 10, "wins": 6, "losses": 4, "open": 0, "win_rate": 60.0, "avg_r": 0.4,
            "net_r": net_r, "max_drawdown_r": 2.0, "calmar": 2.0}


def test_sweep_runs_round_trip_their_params(database):
    run_id = database.log_sweep_run(1, "XAUUSD", "scalp", 30, "net_r", ["min_rrr", "sl_mult"], 5, 1.2,
                                    [_row("rsi_reversion", 4.0)])
    assert json.loads(database.get_sweep_run(run_id)["axes_json"]) == ["min_rrr", "sl_mult"]
    res = database.get_sweep_results(run_id)
    assert res[0]["rank"] == 1 and res[0]["params"] == _row("x", 0)["params"]
    assert database.get_latest_sweep_run(1)["id"] == run_id and database.count_sweep_runs(1) == 1
    assert database.get_latest_sweep_run(2) is None


def test_pre_modular_sweep_rows_are_mapped_onto_current_param_names(database):
    with database.get_db_connection() as conn:
        conn.execute("INSERT INTO sweep_runs (chat_id, symbol, timeframe_mode, days, rank_mode) "
                     "VALUES (1, 'XAUUSD', 'scalp', 30, 'net_r')")
        conn.execute("INSERT INTO sweep_results (run_id, rank, strategy, min_rrr, min_confluence_score, "
                     "sl_atr_mult, rsi_buy, rsi_sell) VALUES (1, 1, 'smc_confluence', 1.5, 35, 1.2, 30, 70)")
        conn.commit()
    assert database.get_sweep_results(1)[0]["params"] == {
        "min_rrr": 1.5, "min_score": 35, "sl_mult": 1.2, "rsi_buy": 30, "rsi_sell": 70}


def test_ranked_history_orders_by_the_top_result_and_rejects_unsafe_sort_input(database):
    low = database.log_sweep_run(1, "XAUUSD", "scalp", 30, "net_r", ["min_rrr"], 5, 1.0, [_row("a", 1.0)])
    high = database.log_sweep_run(1, "XAUUSD", "scalp", 30, "net_r", ["min_rrr"], 5, 1.0, [_row("b", 5.0)])
    ranked = database.get_sweep_history_ranked(1, sort_column="net_r", sort_dir="DESC")
    assert [r["id"] for r in ranked] == [high, low]

    with pytest.raises(ValueError):
        database.get_sweep_history_ranked(1, sort_column="net_r; DROP TABLE signals", sort_dir="DESC")
    with pytest.raises(ValueError):
        database.get_sweep_history_ranked(1, sort_column="net_r", sort_dir="sideways")