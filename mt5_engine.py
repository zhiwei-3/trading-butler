import logging
import pandas as pd
import MetaTrader5 as mt5

def init_mt5():
    if not mt5.initialize():
        logging.error(f"MT5 Initialization failed: {mt5.last_error()}")
        return False
    return True

def check_mt5_alive() -> bool:
    try:
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
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    return df