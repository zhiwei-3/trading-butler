# tests/test_outcomes.py
from core.outcomes import resolve, reconcile_request, be_retry_needed


def make(**kw):
    base = dict(id=1, status="PENDING", direction="BUY", entry_price=2000.0, sl_price=1990.0,
                tp1_price=2015.0, tp2_price=2030.0, dual_entry=0,
                ticket=None, exec_mode="NONE", ticket2=None, exec_mode2="NONE", be_moved2=0)
    base.update(kw)
    return base


BUY = dict(direction="BUY", entry_price=2000.0, sl_price=1990.0, tp1_price=2015.0, tp2_price=2030.0)
SELL = dict(direction="SELL", entry_price=2000.0, sl_price=2010.0, tp1_price=1985.0, tp2_price=1970.0)
LIVE_DUAL = dict(dual_entry=1, exec_mode="LIVE", ticket=11, exec_mode2="LIVE", ticket2=12, **BUY)


def test_paper_single_leg_resolves_from_price():
    assert resolve(make(**BUY), 2015.0).new_status == "CLOSED_TP1"
    assert resolve(make(**BUY), 1990.0).new_status == "HIT_SL"
    assert resolve(make(**BUY), 2005.0) is None
    assert resolve(make(**SELL), 1985.0).new_status == "CLOSED_TP1"
    assert resolve(make(**SELL), 2010.0).new_status == "HIT_SL"


def test_paper_dual_tp1_starts_the_runner_without_broker_actions():
    t = resolve(make(dual_entry=1, **BUY), 2016.0)
    assert t.new_status == "HIT_TP1"
    assert not t.trail_leg2_to_be and t.close_leg2_reason is None


def test_live_dual_tp1_trails_leg2_once():
    t = resolve(make(**LIVE_DUAL), 2016.0, broker_exit="WON_TP1")
    assert t.new_status == "HIT_TP1" and t.trail_leg2_to_be
    assert not resolve(make(be_moved2=1, **LIVE_DUAL), 2016.0, broker_exit="WON_TP1").trail_leg2_to_be


def test_live_legs_are_resolved_only_by_the_broker_never_by_price():
    live = dict(exec_mode="LIVE", ticket=11, **BUY)
    assert resolve(make(**live), 2100.0) is None          # far past TP1, but the broker hasn't confirmed
    assert resolve(make(**live), 1900.0) is None          # far past SL, same


def test_stop_loss_and_manual_close_force_close_a_live_runner():
    t = resolve(make(**LIVE_DUAL), 1980.0, broker_exit="HIT_SL")
    assert t.new_status == "HIT_SL" and t.close_leg2_reason
    t = resolve(make(**LIVE_DUAL), 1980.0, broker_exit="MANUAL")
    assert t.new_status == "CLOSED_MANUAL" and t.close_leg2_reason
    single = resolve(make(exec_mode="LIVE", ticket=11, **BUY), 1980.0, broker_exit="HIT_SL")
    assert single.close_leg2_reason is None                # nothing to close on a single-leg trade


def test_runner_outcomes_paper_and_live():
    run = dict(status="HIT_TP1", dual_entry=1, **SELL)
    assert resolve(make(**run), 1970.0).new_status == "HIT_TP2"
    assert resolve(make(**run), 2000.0).new_status == "CLOSED_BE"
    assert resolve(make(**run), 1990.0) is None

    live = dict(status="HIT_TP1", dual_entry=1, exec_mode2="LIVE", ticket2=12, **BUY)
    assert resolve(make(**live), 2031.0, broker_exit="HIT_TP2").new_status == "HIT_TP2"
    assert resolve(make(**live), 2031.0, broker_exit="CLOSED_BE").new_status == "CLOSED_BE"
    assert resolve(make(**live), 2031.0, broker_exit="MANUAL").new_status == "CLOSED_MANUAL"
    assert resolve(make(**live), 2031.0) is None


def test_terminal_statuses_never_transition():
    for status in ("HIT_SL", "CLOSED_TP1", "HIT_TP2", "CLOSED_BE", "CLOSED_MANUAL", "SKIPPED"):
        assert resolve(make(status=status, **BUY), 1900.0) is None


def test_reconcile_request_and_be_retry():
    assert reconcile_request(make(**BUY)) is None                                   # paper leg
    ticket, cands = reconcile_request(make(**LIVE_DUAL))
    assert ticket == 11 and set(cands) == {"HIT_SL", "WON_TP1"}
    ticket, cands = reconcile_request(make(status="HIT_TP1", **LIVE_DUAL))
    assert ticket == 12 and set(cands) == {"CLOSED_BE", "HIT_TP2"}

    assert be_retry_needed(make(status="HIT_TP1", **LIVE_DUAL))
    assert not be_retry_needed(make(status="HIT_TP1", be_moved2=1, **LIVE_DUAL))
    assert not be_retry_needed(make(**LIVE_DUAL))                                   # still PENDING