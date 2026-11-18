# tests/test_guards.py
import asyncio
import types

import pytest

from config import ALERT_STATE
from core import guards
from core.guards import GuardContext, cooldown, spread, volume


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(guards, "GUARDS", {})
    monkeypatch.setattr(guards, "_STATE", {})


def run(coro):
    return asyncio.run(coro)


def test_scopes_are_separate_and_every_guard_runs(isolated):
    seen = []

    @guards.guard("a", scope="scan", order=20)
    async def a(ctx):
        seen.append("a")
        return "a is blocking"

    @guards.guard("b", scope="scan", order=10)
    async def b(ctx):
        seen.append("b")
        return "b is blocking"

    @guards.guard("c", scope="signal")
    async def c(ctx):
        seen.append("c")
        return None

    blocks = run(guards.run_guards("scan", GuardContext("XAUUSD")))
    assert seen == ["b", "a"]                                   # ordered, and no short-circuit after the first block
    assert [b_.name for b_ in blocks] == ["b", "a"]
    assert run(guards.run_guards("signal", GuardContext("XAUUSD"))) == []


def test_a_crashing_guard_fails_closed(isolated):
    @guards.guard("boom", scope="scan")
    async def boom(ctx):
        raise RuntimeError("bug")

    blocks = run(guards.run_guards("scan", GuardContext("XAUUSD")))
    assert len(blocks) == 1 and "failing closed" in blocks[0].reason


def test_duplicate_or_unknown_scope_is_rejected(isolated):
    @guards.guard("x")
    async def x(ctx):
        return None

    with pytest.raises(ValueError):
        guards.guard("x")(x)
    with pytest.raises(ValueError):
        guards.guard("y", scope="nope")


def test_active_blocks_expire(isolated, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(guards, "_now", lambda: now[0])

    @guards.guard("g")
    async def g(ctx):
        return "blocked"

    run(guards.run_guards("scan", GuardContext("XAUUSD")))
    assert guards.active_blocks() == {"g": "blocked"}
    now[0] += 301
    assert guards.active_blocks() == {}


def test_volume_guard(monkeypatch):
    monkeypatch.setitem(ALERT_STATE, "require_volume_atr_filter", True)
    assert run(volume.check(GuardContext("X", {"vol_filter_ok": False})))
    assert run(volume.check(GuardContext("X", {"vol_filter_ok": True}))) is None
    monkeypatch.setitem(ALERT_STATE, "require_volume_atr_filter", False)
    assert run(volume.check(GuardContext("X", {"vol_filter_ok": False}))) is None


def test_cooldown_guard(monkeypatch):
    monkeypatch.setattr(cooldown, "get_sl_cooldown_remaining_s", lambda: 600.0)
    assert "10m" in run(cooldown.check(GuardContext("X")))
    monkeypatch.setattr(cooldown, "get_sl_cooldown_remaining_s", lambda: 0.0)
    assert run(cooldown.check(GuardContext("X"))) is None


def test_spread_guard(monkeypatch):
    monkeypatch.setitem(ALERT_STATE, "max_allowed_spread_pips", 10.0)
    tick = lambda bid, ask: types.SimpleNamespace(bid=bid, ask=ask)
    monkeypatch.setattr(spread, "get_tick", lambda sym: tick(2000.00, 2002.00))     # 20 pips
    assert "20.0" in run(spread.check(GuardContext("X")))
    monkeypatch.setattr(spread, "get_tick", lambda sym: tick(2000.00, 2000.50))     # 5 pips
    assert run(spread.check(GuardContext("X"))) is None
    monkeypatch.setattr(spread, "get_tick", lambda sym: None)                       # no tick -> cannot judge
    assert run(spread.check(GuardContext("X"))) is None