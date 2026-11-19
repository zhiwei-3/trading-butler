# core/execution/broker.py
"""Broker quirks: which filling mode it accepts, minimum stop distance, volume step."""

import math

from core.mt5 import mt5

_FILLING_CACHE = {}   # symbol -> filling mode that the broker actually accepted


def remember_filling(symbol, mode):
    _FILLING_CACHE[symbol] = mode


def forget_filling(symbol):
    _FILLING_CACHE.pop(symbol, None)


def filling_modes(symbol, info):
    cached = _FILLING_CACHE.get(symbol)
    if cached is not None:
        return [cached]
    mask = getattr(info, "filling_mode", 0) or 0
    ordered = []
    if mask & getattr(mt5, "SYMBOL_FILLING_FOK", 1):
        ordered.append(mt5.ORDER_FILLING_FOK)
    if mask & getattr(mt5, "SYMBOL_FILLING_IOC", 2):
        ordered.append(mt5.ORDER_FILLING_IOC)
    for mode in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
        if mode not in ordered:
            ordered.append(mode)
    return ordered


def adjust_levels(info, direction, price, sl, tp):
    """Pushes SL/TP outside the broker's minimum stop distance, then rounds to digits."""
    point = info.point or 0.01
    digits = info.digits
    min_dist = max(int(getattr(info, "trade_stops_level", 0) or 0), 0) * point
    if direction == "BUY":
        if sl is not None and (price - sl) < min_dist:
            sl = price - min_dist
        if tp is not None and (tp - price) < min_dist:
            tp = price + min_dist
    else:
        if sl is not None and (sl - price) < min_dist:
            sl = price + min_dist
        if tp is not None and (price - tp) < min_dist:
            tp = price - min_dist
    return (round(sl, digits) if sl is not None else None,
            round(tp, digits) if tp is not None else None)


def snap_volume(info, volume):
    step = info.volume_step or 0.01
    vol = math.floor((volume / step) + 1e-9) * step
    return round(max(info.volume_min, min(info.volume_max, vol)), 2)