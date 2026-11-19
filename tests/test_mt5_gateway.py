# tests/test_mt5_gateway.py
import threading
import types

import pytest

from core.mt5.gateway import MT5_LOCK, LockedMt5


def _lock_free_for_other_threads():
    got = []

    def probe():
        ok = MT5_LOCK.acquire(blocking=False)
        got.append(ok)
        if ok:
            MT5_LOCK.release()

    t = threading.Thread(target=probe)
    t.start()
    t.join()
    return got[0]


def test_every_function_call_runs_under_the_lock():
    seen = []
    fake = types.SimpleNamespace(
        TIMEFRAME_M1=1,
        positions_get=lambda: seen.append(_lock_free_for_other_threads()) or [],
    )
    mt5 = LockedMt5(fake)
    assert mt5.TIMEFRAME_M1 == 1                      # constants pass straight through
    mt5.positions_get()
    assert seen == [False]                            # another thread could NOT enter mid-call
    assert _lock_free_for_other_threads() is True     # and the lock is released afterwards


def test_the_lock_is_released_even_when_the_call_raises():
    def boom():
        raise RuntimeError("terminal exploded")
    mt5 = LockedMt5(types.SimpleNamespace(order_send=boom))
    with pytest.raises(RuntimeError):
        mt5.order_send()
    assert _lock_free_for_other_threads() is True


def test_nested_locking_is_safe():
    mt5 = LockedMt5(types.SimpleNamespace(terminal_info=lambda: "ok"))
    with MT5_LOCK:                                    # callers may group several calls atomically
        assert mt5.terminal_info() == "ok"


def test_classes_pass_through_unwrapped():
    class Info:
        pass
    assert LockedMt5(types.SimpleNamespace(Info=Info)).Info is Info


def test_missing_names_raise_attributeerror_so_getattr_defaults_work():
    mt5 = LockedMt5(types.SimpleNamespace())
    assert getattr(mt5, "NOPE", 42) == 42
    assert not hasattr(mt5, "NOPE")


def test_without_the_package_usage_fails_loudly_but_import_time_defaults_work():
    mt5 = LockedMt5(None)
    assert getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030) == 10030
    with pytest.raises(AttributeError, match="not installed"):
        mt5.terminal_info()