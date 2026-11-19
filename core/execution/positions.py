# core/execution/positions.py
import MetaTrader5 as mt5

from core.execution.common import magic
from core.mt5_engine import MT5_LOCK


def fetch_managed(symbol=None):
    """
    Returns the list of this bot's positions, or None if MT5 could not be queried.
    (None must NOT be treated as 'no positions' — that was the fail-open bug.)
    """
    with MT5_LOCK:
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        err = mt5.last_error() if positions is None else None
    if positions is None:
        # Some terminal builds return None + (1, 'Success') for "no positions".
        if err and err[0] == getattr(mt5, "RES_S_OK", 1):
            return []
        return None
    return [p for p in positions if p.magic == magic()]


def list_managed_positions(symbol=None):
    """Only positions carrying this bot's magic number — manual trades are untouchable."""
    return fetch_managed(symbol) or []


def position_by_ticket(ticket):
    with MT5_LOCK:
        positions = mt5.positions_get(ticket=int(ticket))
    return positions[0] if positions else None