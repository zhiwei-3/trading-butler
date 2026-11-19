# core/execution/intents.py
"""The queue strategies push trade ideas into. Pure bookkeeping: never touches MT5."""

import logging
import threading
from datetime import datetime, timezone

from config import ALERT_STATE

_LOCK = threading.Lock()
_PENDING = []


def request_execution(signal_id, symbol, direction, entry, sl_price, tp1_price, tp2_price, score,
                      dual_entry=False, lots=None):
    """Called by the strategy layer. Queues an intent; never touches MT5 itself."""
    if not ALERT_STATE.get("auto_trade_enabled", False):
        return False
    if not signal_id:
        logging.error("request_execution: signal was not logged to the DB — refusing to open an untracked trade.")
        return False
    with _LOCK:
        _PENDING.append({
            "signal_id": signal_id, "symbol": symbol, "direction": direction,
            "entry": float(entry), "sl": float(sl_price),
            "tp1": float(tp1_price), "tp2": float(tp2_price),
            "score": score, "dual_entry": bool(dual_entry),
            "lots": float(lots) if lots else None,      # None -> fixed_lot_size
            "queued_at": datetime.now(timezone.utc),
        })
    return True


def pending_intent_count():
    with _LOCK:
        return len(_PENDING)


def take_pending():
    """Atomically removes and returns every queued intent."""
    with _LOCK:
        batch = list(_PENDING)
        _PENDING[:] = []
    return batch