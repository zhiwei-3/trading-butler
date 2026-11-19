# core/mt5/__init__.py
"""MT5 access layer. Everything that talks to the terminal goes through here.

    from core.mt5 import mt5                 # lock-protected proxy of the MetaTrader5 module
    from core.mt5 import fetch_candles, get_tick, init_mt5, ...
"""

from core.mt5.gateway import mt5, MT5_LOCK
from core.mt5.terminal import init_mt5, check_mt5_alive, get_gold_symbol
from core.mt5.market import tf_const, fetch_candles, fetch_history, get_tick
from core.mt5.clock import get_broker_utc_offset_seconds

__all__ = [
    "mt5", "MT5_LOCK", "init_mt5", "check_mt5_alive", "get_gold_symbol",
    "tf_const", "fetch_candles", "fetch_history", "get_tick", "get_broker_utc_offset_seconds",
]