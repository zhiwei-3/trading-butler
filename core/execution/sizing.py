# core/execution/sizing.py
import math
import logging

from core.mt5 import mt5
from core.mt5 import MT5_LOCK, init_mt5


def calc_risk_lots(symbol, sl_dist, legs=1, risk_pct=0.5, max_lot=1.0):
    """
    BLOCKING. Volatility-scaled lot size per leg:

        lots = (balance * risk% / legs) / ((stop_distance / tick_size) * tick_value)

    stop_distance = the strategy's actual stop, so higher ATR -> wider stop -> fewer lots,
    lower ATR -> more lots, and the money at risk stays constant. risk_pct and max_lot come
    from the strategy's own params (see strategies._lib.sizing_params).
    Returns {"ok", "lots", "risk_money", "balance", "reason"}.
    """
    fail = lambda why: {"ok": False, "lots": None, "reason": why}
    try:
        if not sl_dist or sl_dist <= 0:
            return fail("Stop distance is zero.")
        if not init_mt5():
            return fail("MT5 terminal unreachable - cannot size the position.")
        with MT5_LOCK:
            acc = mt5.account_info()
            info = mt5.symbol_info(symbol)
        if acc is None or info is None:
            return fail("Account or symbol info unavailable for sizing.")

        tick_size = float(info.trade_tick_size or info.point or 0)
        tick_value = float(getattr(info, "trade_tick_value_loss", 0) or info.trade_tick_value or 0)
        if tick_size <= 0 or tick_value <= 0:
            return fail("Broker reports no tick size/value for this symbol.")

        budget = float(acc.balance) * float(risk_pct) / 100.0 / max(1, legs)
        loss_per_lot = (float(sl_dist) / tick_size) * tick_value
        raw = budget / loss_per_lot

        step = info.volume_step or 0.01
        lots = math.floor(raw / step + 1e-9) * step
        lots = round(min(lots, float(max_lot), info.volume_max), 2)
        if lots < info.volume_min - 1e-9:
            return fail(f"Risk budget too small: {risk_pct}% of balance gives {raw:.4f} lots "
                        f"(broker minimum {info.volume_min}).")
        return {"ok": True, "lots": lots, "balance": float(acc.balance),
                "risk_money": round(lots * loss_per_lot * legs, 2), "reason": ""}
    except Exception as e:
        logging.exception("calc_risk_lots failed")
        return fail(f"Sizing error: {e}")