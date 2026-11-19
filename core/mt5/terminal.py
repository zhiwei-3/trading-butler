# core/mt5/terminal.py
import logging

from core.mt5.gateway import mt5, MT5_LOCK


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