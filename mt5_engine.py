import logging
import threading
import pandas as pd
import MetaTrader5 as mt5

# MetaTrader5's Python module is NOT thread-safe: concurrent calls from multiple
# threads (e.g. the backtester running in a worker thread via asyncio.to_thread
# while a scheduled job fires on the main event loop at the same moment) can hang
# or corrupt the terminal connection with no exception raised. RLock (not Lock) is
# used because some of these functions call each other and may re-acquire it from
# the same thread. Every MT5 call anywhere in the app should go through this lock.
MT5_LOCK = threading.RLock()

def init_mt5():
    with MT5_LOCK:
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
    df['time'] = pd.to_datetime(df['time'], unit='s')
    return df