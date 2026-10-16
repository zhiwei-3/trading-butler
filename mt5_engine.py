import time
import logging
import threading
import pandas as pd
import MetaTrader5 as mt5

from config import ALERT_STATE, save_settings

# MetaTrader5's Python module is NOT thread-safe: concurrent calls from multiple
# threads (e.g. the backtester running in a worker thread via asyncio.to_thread
# while a scheduled job fires on the main event loop at the same moment) can hang
# or corrupt the terminal connection with no exception raised. RLock (not Lock) is
# used because some of these functions call each other and may re-acquire it from
# the same thread. Every MT5 call anywhere in the app should go through this lock.
MT5_LOCK = threading.RLock()

def init_mt5():
    with MT5_LOCK:
        if mt5.terminal_info() is None:
            logging.warning("⚠️ MT5 connection lost. Re-initializing...")
            mt5.shutdown()
            if not mt5.initialize():
                logging.error(f"MT5 Initialization failed: {mt5.last_error()}")
                return False
        return True

def check_mt5_alive() -> bool:
    try:
        with MT5_LOCK:
            info = mt5.terminal_info()
            if info is None:
                init_mt5()
                info = mt5.terminal_info()
            return bool(info and info.connected)
    except Exception:
        return False

def get_gold_symbol():
    if not init_mt5():
        return None
    with MT5_LOCK:
        candidates = ["XAUUSD", "XAUUSD.a", "GOLD", "XAUUSDm"]
        for sym in candidates:
            if mt5.symbol_select(sym, True):
                return sym
        symbols = mt5.symbols_get(group="*XAU*")
        if symbols:
            mt5.symbol_select(symbols[0].name, True)
            return symbols[0].name
        return None

def fetch_candles(symbol, timeframe, count=100):
    with MT5_LOCK:
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    # BUGFIX: the backtester builds tz-AWARE UTC timestamps for the same 'time'
    # column; leaving this tz-naive made the two code paths silently disagree on
    # what a given bar's timestamp means whenever they're compared.
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    return df

def get_tick(symbol):
    """Locked wrapper around symbol_info_tick — callers must never call the raw
    MT5 function directly (see the thread-safety note above)."""
    if not symbol:
          logging.warning("get_tick called with None/empty symbol")
          return None
    with MT5_LOCK:
        return mt5.symbol_info_tick(symbol)

_OFFSET_CACHE = {"value": None, "at": 0.0}
_OFFSET_TTL_SECONDS = 300

def get_broker_utc_offset_seconds():
    """
    MT5 candle/tick timestamps are BROKER SERVER time (epoch labelled as UTC).
    Everything the bot stores in SQLite is true UTC, so chart overlays need the
    server's offset to line trades up with candles.

    Detection: a FRESH tick's `time` minus real UTC now is a whole number of
    hours. A stale tick (weekend / market closed) is rejected and we fall back
    to the last known-good offset persisted in settings.json.
    Returns seconds (add to a UTC epoch to get broker-server epoch).
    """
    now = time.time()
    if _OFFSET_CACHE["value"] is not None and (now - _OFFSET_CACHE["at"]) < _OFFSET_TTL_SECONDS:
        return _OFFSET_CACHE["value"]

    detected_h = None
    try:
        sym = get_gold_symbol()
        tick = get_tick(sym) if sym else None
        if tick is not None and getattr(tick, "time", 0):
            raw_h = (tick.time - now) / 3600.0
            # fresh tick => within ~6 min of a whole-hour offset, and a sane timezone
            if abs(raw_h) <= 14 and abs(raw_h - round(raw_h)) <= 0.1:
                detected_h = int(round(raw_h))
    except Exception as e:
        logging.warning(f"Broker offset detection failed: {e}")

    if detected_h is not None:
        if ALERT_STATE.get("broker_utc_offset_hours") != detected_h:
            ALERT_STATE["broker_utc_offset_hours"] = detected_h
            save_settings()
        value = detected_h * 3600
    else:
        saved = ALERT_STATE.get("broker_utc_offset_hours")
        value = int(float(saved) * 3600) if saved is not None else 0

    _OFFSET_CACHE["value"], _OFFSET_CACHE["at"] = value, now
    return value
