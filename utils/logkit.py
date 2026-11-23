# utils/logkit.py
"""Structured key=value event logging. Pure: only the stdlib logging module.
    audit("cmd", cmd="set", ms=12)            -> logger tb.audit   (user activity)
    trade_event("order.open", ok=True, ...)   -> logger tb.trade   (trade lifecycle)
    system("job.failed", job="scanner")       -> logger tb.system  (bot internals)
Fields with value None are omitted. Strings with spaces/quotes are JSON-quoted."""

import json
import logging

AUDIT = logging.getLogger("tb.audit")
TRADE = logging.getLogger("tb.trade")
SYSTEM = logging.getLogger("tb.system")

_MAX_LEN = 240


def _v(value):
    s = str(value).replace("\n", " ⏎ ").strip()
    if len(s) > _MAX_LEN:
        s = s[:_MAX_LEN - 1] + "…"
    if not s or any(c in s for c in ' ="'):
        return json.dumps(s, ensure_ascii=False)
    return s


def kv(**fields):
    return " ".join(f"{k}={_v(v)}" for k, v in fields.items() if v is not None)


def _emit(logger, event, lvl, fields):
    text = kv(**fields)
    logger.log(lvl, f"{event} {text}" if text else event)


def audit(event, _lvl=logging.INFO, **fields):
    _emit(AUDIT, event, _lvl, fields)


def trade_event(event, _lvl=logging.INFO, **fields):
    _emit(TRADE, event, _lvl, fields)


def system(event, _lvl=logging.INFO, **fields):
    _emit(SYSTEM, event, _lvl, fields)