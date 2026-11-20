# core/execution/enforcer.py
from config import ALERT_STATE
from core.db import get_active_live_signals, set_signal_status
from core.execution.common import EXEC_LOCK
from core.execution.orders import close_position
from core.execution.positions import fetch_managed


def enforce_single_trade():
    """
    Safety net. The only legal live state is ONE signal's legs (1 or 2 tickets).
    Closes (a) bot positions not linked to any active signal (e.g. retry duplicates)
    and (b) every active signal after the oldest one. Returns report lines.
    """
    if ALERT_STATE.get("trade_dry_run", True):
        return []
    with EXEC_LOCK:
        positions = fetch_managed()
        if not positions:
            return []
        signals = get_active_live_signals()          # oldest first
        keep = signals[0] if signals else None
        keep_tickets = {t for t in (keep["ticket"], keep["ticket2"]) if t} if keep else set()

        out = []
        for p in sorted(positions, key=lambda p: p.time):
            if int(p.ticket) in keep_tickets:
                continue
            res = close_position(p.ticket)
            out.append(f"🛑 **Extra position `#{p.ticket}` closed** (one-trade rule) — "
                       f"{'✅' if res['ok'] else '⚠️'} {res['reason']}")
        for extra in signals[1:]:
            set_signal_status(extra["id"], "SKIPPED")
        return out