# tests/test_execution.py
import types

import pytest

from config import ALERT_STATE
from core.execution import broker, intents, pnl, reconcile, sizing


def ns(**kw):
    return types.SimpleNamespace(**kw)


INFO = ns(point=0.01, digits=2, trade_stops_level=50, volume_step=0.01, volume_min=0.01, volume_max=100.0,
          trade_tick_size=0.01, trade_tick_value=1.0, trade_contract_size=100.0)


# ---------------------------------------------------------------- broker quirks

def test_adjust_levels_pushes_stops_outside_the_minimum_distance():
    # stops_level 50 pts * 0.01 = 0.50 minimum distance
    assert broker.adjust_levels(INFO, "BUY", 2000.0, 1999.8, 2000.2) == (1999.5, 2000.5)
    assert broker.adjust_levels(INFO, "SELL", 2000.0, 2000.2, 1999.8) == (2000.5, 1999.5)
    assert broker.adjust_levels(INFO, "BUY", 2000.0, 1990.0, 2015.0) == (1990.0, 2015.0)   # already far enough
    assert broker.adjust_levels(INFO, "BUY", 2000.0, None, 2015.0) == (None, 2015.0)


def test_snap_volume_floors_to_step_and_respects_limits():
    assert broker.snap_volume(INFO, 0.137) == 0.13
    assert broker.snap_volume(INFO, 0.004) == 0.01          # raised to the broker minimum
    assert broker.snap_volume(INFO, 500) == 100.0           # capped at the broker maximum


# ---------------------------------------------------------------- sizing

@pytest.fixture
def sized(monkeypatch):
    def setup(balance=10000.0, **overrides):
        fake = ns(account_info=lambda: ns(balance=balance),
                  symbol_info=lambda s: ns(**{**INFO.__dict__, **overrides}))
        monkeypatch.setattr(sizing, "mt5", fake)
        monkeypatch.setattr(sizing, "init_mt5", lambda: True)
    return setup


def test_risk_lots_keep_the_money_at_risk_constant(sized):
    sized()                                    # 0.5% of 10,000 = 50; a 5-point stop costs 500 per lot
    r = sizing.calc_risk_lots("XAUUSD", 5.0, risk_pct=0.5, max_lot=1.0)
    assert r["ok"] and r["lots"] == 0.1 and r["risk_money"] == 50.0

    r2 = sizing.calc_risk_lots("XAUUSD", 5.0, legs=2, risk_pct=0.5, max_lot=1.0)   # budget shared between legs
    assert r2["lots"] == 0.05 and r2["risk_money"] == 50.0


def test_risk_lots_are_capped_and_can_refuse(sized):
    sized()
    assert sizing.calc_risk_lots("XAUUSD", 5.0, risk_pct=0.5, max_lot=0.03)["lots"] == 0.03

    assert not sizing.calc_risk_lots("XAUUSD", 0)["ok"]
    sized(balance=100.0)
    r = sizing.calc_risk_lots("XAUUSD", 50.0, risk_pct=0.5, max_lot=1.0)   # budget 0.5 -> 0.0001 lots, below minimum
    assert not r["ok"] and "too small" in r["reason"]


# ---------------------------------------------------------------- reconciliation

def _fake_deals(monkeypatch, deals, still_open=False):
    fake = ns(history_deals_get=lambda *a, **k: deals, DEAL_ENTRY_OUT=1, DEAL_REASON_SL=4, DEAL_REASON_TP=5)
    monkeypatch.setattr(reconcile, "mt5", fake)
    monkeypatch.setattr(reconcile, "position_by_ticket", lambda t: object() if still_open else None)


LEVELS = {"HIT_SL": 1990.0, "WON_TP1": 2015.0}


def test_reconcile_uses_the_brokers_close_reason(monkeypatch):
    _fake_deals(monkeypatch, [ns(entry=1, time=1, price=1990.0, reason=4, position_id=11)])
    assert reconcile.reconcile_exit(11, LEVELS) == "HIT_SL"
    _fake_deals(monkeypatch, [ns(entry=1, time=1, price=2015.0, reason=5, position_id=11)])
    assert reconcile.reconcile_exit(11, LEVELS) == "WON_TP1"
    _fake_deals(monkeypatch, [ns(entry=1, time=1, price=2003.0, reason=0, position_id=11)])
    assert reconcile.reconcile_exit(11, LEVELS) == "MANUAL"          # a click in the terminal is never SL/TP


def test_reconcile_without_a_reason_needs_the_exit_near_a_level(monkeypatch):
    _fake_deals(monkeypatch, [ns(entry=1, time=1, price=2014.0, position_id=11)])
    assert reconcile.reconcile_exit(11, LEVELS) == "WON_TP1"
    _fake_deals(monkeypatch, [ns(entry=1, time=1, price=2003.0, position_id=11)])
    assert reconcile.reconcile_exit(11, LEVELS) == "MANUAL"


def test_reconcile_waits_while_open_or_without_history(monkeypatch):
    _fake_deals(monkeypatch, [ns(entry=1, time=1, price=1990.0, reason=4, position_id=11)], still_open=True)
    assert reconcile.reconcile_exit(11, LEVELS) is None
    _fake_deals(monkeypatch, [])
    assert reconcile.reconcile_exit(11, LEVELS) is None


# ---------------------------------------------------------------- P&L blocks

def _sig(**kw):
    base = dict(direction="BUY", symbol="XAUUSD", entry_price=2000.0, sl_price=1990.0, tp1_price=2015.0,
                tp2_price=2030.0, dual_entry=0, exec_mode="DRY", ticket=None, lots=0.01, fill_price=2000.0,
                exec_mode2="NONE", ticket2=None, lots2=None, fill_price2=None)
    base.update(kw)
    return base


@pytest.fixture
def usd(monkeypatch):
    monkeypatch.setattr(pnl, "mt5", ns(account_info=lambda: ns(currency="USD"), symbol_info=lambda s: INFO))


def test_single_leg_win_is_simulated_from_the_signal_levels(usd):
    out = pnl.summarize_outcome_pnl(_sig(), "CLOSED_TP1")          # 15 pts * 0.01 lots * 100 oz
    assert "+15.00 USD" in out and "+1.50R" in out and "🧪" in out


def test_dual_stop_out_totals_both_legs(usd):
    sig = _sig(dual_entry=1, exec_mode2="DRY", lots2=0.01, fill_price2=2000.0)
    out = pnl.summarize_outcome_pnl(sig, "HIT_SL")
    assert out.count("-10.00 USD") == 2 and "Trade net" in out and "-20.00 USD" in out and "-1.00R" in out


def test_runner_note_and_unknown_or_manual_statuses(usd):
    sig = _sig(dual_entry=1, exec_mode2="DRY", lots2=0.01, fill_price2=2000.0)
    assert "Runner still open" in pnl.summarize_outcome_pnl(sig, "HIT_TP1")
    assert pnl.summarize_outcome_pnl(_sig(), "SOMETHING_ELSE") == ""
    assert pnl.summarize_outcome_pnl(_sig(), "CLOSED_MANUAL") == ""      # nothing live to report on


# ---------------------------------------------------------------- intent queue

@pytest.fixture
def queue(monkeypatch):
    monkeypatch.setattr(intents, "_PENDING", [])
    monkeypatch.setitem(ALERT_STATE, "auto_trade_enabled", True)


def test_intents_are_only_queued_when_armed_and_tracked(queue, monkeypatch):
    args = (1, "XAUUSD", "BUY", 2000, 1990, 2015, 2030, 70)
    assert intents.request_execution(None, *args[1:]) is False          # untracked signal: refused
    monkeypatch.setitem(ALERT_STATE, "auto_trade_enabled", False)
    assert intents.request_execution(*args) is False                    # disarmed: refused
    monkeypatch.setitem(ALERT_STATE, "auto_trade_enabled", True)
    assert intents.request_execution(*args, dual_entry=True, lots=0.05) is True
    assert intents.pending_intent_count() == 1


def test_take_pending_drains_atomically(queue):
    intents.request_execution(1, "XAUUSD", "BUY", 2000, 1990, 2015, 2030, 70)
    batch = intents.take_pending()
    assert len(batch) == 1 and batch[0]["signal_id"] == 1 and batch[0]["dual_entry"] is False
    assert intents.pending_intent_count() == 0 and intents.take_pending() == []