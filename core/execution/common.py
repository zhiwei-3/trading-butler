# core/execution/common.py
"""Shared constants, setting readers and tiny cross-module state for the execution package."""

import time
import threading

from core.mt5 import mt5

from config import ALERT_STATE

MAX_SEND_RETRIES = 3
FILL_COOLDOWN_S = 20                 # block new trade ideas this long after any fill

EXEC_LOCK = threading.Lock()         # serialises check-and-open so two drains can never interleave

RC_INVALID_FILL = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)
RC_RETRYABLE = {
    getattr(mt5, "TRADE_RETCODE_REQUOTE", 10004),
    getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", 10020),
    getattr(mt5, "TRADE_RETCODE_PRICE_OFF", 10021),
    getattr(mt5, "TRADE_RETCODE_CONNECTION", 10031),
}
_RC_TEXT = {
    10004: "Requote",
    10006: "Request rejected by broker",
    10013: "Invalid request",
    10014: "Invalid volume",
    10015: "Invalid price",
    10016: "Invalid stops (SL/TP too close or wrong side)",
    10018: "Market is closed",
    10019: "Insufficient free margin",
    10027: "AutoTrading disabled in terminal",
    10028: "AutoTrading disabled by server",
    10030: "Unsupported filling mode",
    10031: "No connection to trade server",
}


def rc_text(rc):
    return _RC_TEXT.get(rc, f"retcode {rc}")


# ---------------------------------------------------------------- settings (read at call time, never cached)

def magic():
    return int(ALERT_STATE.get("magic_number", 770077))


def comment(tag=None):
    return str(tag or ALERT_STATE.get("trade_comment", "TradingButler"))[:31]


def deviation():
    return int(ALERT_STATE.get("max_slippage_points", 30))


def fixed_lot():
    return float(ALERT_STATE.get("fixed_lot_size", 0.01))


# ---------------------------------------------------------------- last-fill timestamp

_last_fill_at = float("-inf")        # monotonic time of the last real fill


def mark_fill():
    global _last_fill_at
    _last_fill_at = time.monotonic()


def seconds_since_fill():
    return time.monotonic() - _last_fill_at