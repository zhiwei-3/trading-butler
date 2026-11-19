# core/execution/reconcile.py
"""Reading back what the broker did: how a leg closed, and what it really made."""

import logging
from datetime import datetime, timezone, timedelta

from core.mt5 import mt5

from core.execution.positions import position_by_ticket
from core.mt5 import MT5_LOCK


def reconcile_exit(ticket, candidates):
    """
    If `ticket`'s position is closed, classify HOW it closed.

    Returns:
      * a label from `candidates` ONLY when the broker closed it at SL/TP
        (deal.reason), or - if the build exposes no reason - when the exit price
        sits near one of the levels;
      * "MANUAL" when it was closed by something else (/close, terminal click,
        flatten, stop-out) - never forced into SL/TP;
      * None if still open or no deal history yet.
    """
    if position_by_ticket(ticket) is not None:
        return None
    try:
        with MT5_LOCK:
            deals = mt5.history_deals_get(position=int(ticket))
        if not deals:
            now = datetime.now(timezone.utc)
            with MT5_LOCK:
                window = mt5.history_deals_get(now - timedelta(days=8), now + timedelta(days=1))
            deals = [d for d in (window or []) if int(d.position_id) == int(ticket)]
    except Exception as e:
        logging.warning(f"Deal history lookup failed for #{ticket}: {e}")
        return None
    if not deals:
        return None

    out_deals = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
    if not out_deals:
        return None
    last_deal = max(out_deals, key=lambda d: d.time)
    exit_price = float(last_deal.price)

    scored = {k: v for k, v in candidates.items() if v is not None}
    if not scored:
        return None
    nearest = min(scored, key=lambda k: abs(exit_price - scored[k]))

    reason = getattr(last_deal, "reason", None)
    if reason is not None:
        broker_levels = (getattr(mt5, "DEAL_REASON_SL", 4), getattr(mt5, "DEAL_REASON_TP", 5))
        return nearest if reason in broker_levels else "MANUAL"

    # Fallback (no deal.reason): require the exit to be close to a level.
    span = max(scored.values()) - min(scored.values())
    tol = span * 0.25 if span > 0 else 0.0
    return nearest if abs(exit_price - scored[nearest]) <= tol else "MANUAL"


def position_realized_pnl(ticket):
    """Net realised P&L (profit + swap + commission + fee over ALL deals) of a CLOSED
    position. None if still open or no history yet."""
    try:
        with MT5_LOCK:
            deals = mt5.history_deals_get(position=int(ticket))
    except Exception as e:
        logging.warning(f"Realised P&L lookup failed for #{ticket}: {e}")
        return None
    if not deals:
        return None
    out_kinds = (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT, mt5.DEAL_ENTRY_OUT_BY)
    if not any(d.entry in out_kinds for d in deals):
        return None
    return round(sum(d.profit + d.swap + d.commission + getattr(d, "fee", 0.0) for d in deals), 2)