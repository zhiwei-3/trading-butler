import math
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

def calculate_position_size(symbol: str, sl_dist_pts: float, risk_pct: float = 1.0) -> dict:
    """Calculates exact lot size based on live MT5 balance and SL distance."""
    with MT5_LOCK:
        acc = mt5.account_info()
        symbol_info = mt5.symbol_info(symbol)

    if acc is None or symbol_info is None or sl_dist_pts <= 0:
        return {"lots": 0.01, "risk_usd": 0.0, "balance": 0.0}

    balance = acc.balance
    risk_usd = balance * (risk_pct / 100.0)

    trade_tick_value = symbol_info.trade_tick_value
    trade_tick_size = symbol_info.trade_tick_size

    if trade_tick_size <= 0:
        return {"lots": symbol_info.volume_min, "risk_usd": risk_usd, "balance": balance}

    ticks_in_sl = sl_dist_pts / trade_tick_size
    cost_per_lot_sl = ticks_in_sl * trade_tick_value

    lots = (risk_usd / cost_per_lot_sl) if cost_per_lot_sl > 0 else symbol_info.volume_min

    step = symbol_info.volume_step or 0.01
    # BUGFIX: round() can round UP, silently sizing the position to risk MORE than
    # risk_pct of the account. Flooring guarantees actual risk <= the requested %.
    lots = math.floor((lots / step) + 1e-9) * step
    lots = max(symbol_info.volume_min, min(symbol_info.volume_max, lots))

    return {
        "lots": round(lots, 2),
        "risk_usd": round(risk_usd, 2),
        "balance": round(balance, 2)
    }