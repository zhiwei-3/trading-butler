# core/mt5/clock.py
import time
import logging

from config import ALERT_STATE, save_settings
from core.mt5.market import get_tick
from core.mt5.terminal import get_gold_symbol

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