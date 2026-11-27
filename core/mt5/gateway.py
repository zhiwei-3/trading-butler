# core/mt5/gateway.py
"""
The ONLY place in the project that imports MetaTrader5 (tests/test_architecture.py enforces this).

MetaTrader5's Python module is NOT thread-safe: concurrent calls from multiple threads (e.g. the
backtester running in a worker thread via asyncio.to_thread while a scheduled job fires on the main
event loop) can hang or corrupt the terminal connection with no exception raised.

`mt5` below is a thin proxy over the real module: constants pass straight through, and every
function call runs under MT5_LOCK. Import it everywhere instead of the raw module:

    from core.mt5 import mt5

MT5_LOCK is an RLock, so code that needs several calls to be atomic (e.g. positions_get() then
last_error()) can still wrap them in `with MT5_LOCK:` — nested per-call locking is harmless.
"""

import functools
import threading

try:
    import MetaTrader5 as _raw          # the ONLY import of MetaTrader5 in the project
except ImportError:                      # the package is Windows-only; pure layers and tests still import fine
    _raw = None

MT5_LOCK = threading.RLock()


class LockedMt5:
    def __init__(self, raw):
        self._raw = raw
        self._wrapped = {}

    def __getattr__(self, name):
        if name.startswith("__") and name not in ("__version__", "__author__"):
            raise AttributeError(name)
        wrapped = self._wrapped.get(name)
        if wrapped is not None:
            return wrapped
        if self._raw is None:
            # AttributeError (not RuntimeError) so `getattr(mt5, "X", default)` keeps working at import time.
            raise AttributeError(f"MetaTrader5 is not installed — cannot use mt5.{name} "
                                 f"(Windows only: pip install MetaTrader5)")
        attr = getattr(self._raw, name)
        if not callable(attr) or isinstance(attr, type):      # constants and classes pass through untouched
            return attr

        @functools.wraps(attr)
        def locked(*args, **kwargs):
            with MT5_LOCK:
                return attr(*args, **kwargs)

        self._wrapped[name] = locked
        return locked


mt5 = LockedMt5(_raw)