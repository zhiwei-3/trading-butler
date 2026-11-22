# core/execution/enforcer.py
from config import ALERT_STATE
from core.db import get_active_live_signals, set_signal_status
from core.execution.common import EXEC_LOCK
from core.execution.orders import close_position
from core.execution.positions import fetch_managed


def _tickets(sig):
    return {int(t) for t in (sig["ticket"], sig["ticket2"]) if t}


def enforce_single_trade():
    """
    Safety net. The only legal live state is ONE signal's legs (1 or 2 tickets).
    Closes (a) bot positions not linked to any active signal (e.g. retry duplicates)
    and (b) positions of every owning signal after the oldest one. Returns report lines.

    Only signals that actually OWN an open position count: an older signal whose
    tickets are already closed (tracker hasn't resolved it yet) must neither be
    "kept" nor be marked SKIPPED, or it would cost us a freshly opened trade.
    """
    if ALERT_STATE.get("trade_dry_run", True):
        return []
    with EXEC_LOCK:
        positions = fetch_managed()
        if not positions:
            return []
        open_tickets = {int(p.ticket) for p in positions}
        signals = get_active_live_signals()          # oldest first
        owners = [s for s in signals if _tickets(s) & open_tickets]
        keep = owners[0] if owners else None
        keep_tickets = _tickets(keep) if keep else set()

        out = []
        for p in sorted(positions, key=lambda p: p.time):
            if int(p.ticket) in keep_tickets:
                continue
            res = close_position(p.ticket)
            out.append(f"🛑 **Extra position `#{p.ticket}` closed** (one-trade rule) — "
                       f"{'✅' if res['ok'] else '⚠️'} {res['reason']}")
        for extra in owners[1:]:
            set_signal_status(extra["id"], "SKIPPED")
        return out