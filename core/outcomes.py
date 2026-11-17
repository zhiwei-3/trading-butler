# core/outcomes.py
"""
Pure signal-outcome state machine. No MT5, DB, Telegram or settings — callers observe the world
(broker deal history, latest price) and pass the observations in; this decides what happened.

Lifecycle:
  PENDING --leg 1 at TP1--> CLOSED_TP1             (single-leg signal: full win)
  PENDING --leg 1 at TP1--> HIT_TP1                (dual signal: leg 2 runs with SL at break-even)
  PENDING --leg 1 at SL---> HIT_SL                 (a live leg 2 shares the SL, so it is force-closed)
  PENDING --closed by hand> CLOSED_MANUAL
  HIT_TP1 --runner at TP2-> HIT_TP2
  HIT_TP1 --runner at BE--> CLOSED_BE
  HIT_TP1 --closed by hand> CLOSED_MANUAL

A LIVE leg is only ever resolved from the broker's answer (never from price): if the broker
hasn't confirmed a close yet, nothing changes. Dry-run / paper legs resolve from price.
"""

from dataclasses import dataclass
from typing import Optional

PENDING = "PENDING"
HIT_TP1 = "HIT_TP1"


@dataclass(frozen=True)
class Transition:
    new_status: str
    message: str                                  # Telegram-Markdown headline for the alert
    trail_leg2_to_be: bool = False                # act: move a live leg 2's SL to break-even
    close_leg2_reason: Optional[str] = None       # act: force-close a live leg 2, with this label


def _live(sig, leg):
    mode, ticket = (sig["exec_mode"], sig["ticket"]) if leg == 1 else (sig["exec_mode2"], sig["ticket2"])
    return mode == "LIVE" and bool(ticket)


def reconcile_request(sig):
    """What the caller must ask the broker about for this signal: (ticket, {label: price}),
    or None when the tracked leg is dry-run/paper (resolved from price instead)."""
    if sig["status"] == PENDING and _live(sig, 1):
        return int(sig["ticket"]), {"HIT_SL": sig["sl_price"], "WON_TP1": sig["tp1_price"]}
    if sig["status"] == HIT_TP1 and _live(sig, 2):
        return int(sig["ticket2"]), {"CLOSED_BE": sig["entry_price"], "HIT_TP2": sig["tp2_price"]}
    return None


def be_retry_needed(sig):
    """A live runner whose break-even trail hasn't been confirmed yet (retried every tick)."""
    return sig["status"] == HIT_TP1 and _live(sig, 2) and not sig["be_moved2"]


# ---------------------------------------------------------------- paper (price-based) exits

def _paper_leg1(direction, price, sl, tp1):
    if direction == "BUY":
        if price >= tp1:
            return "WON_TP1"
        if price <= sl:
            return "HIT_SL"
    else:
        if price <= tp1:
            return "WON_TP1"
        if price >= sl:
            return "HIT_SL"
    return None


def _paper_runner(direction, price, entry, tp2):
    if direction == "BUY":
        if price >= tp2:
            return "HIT_TP2"
        if price <= entry:
            return "CLOSED_BE"
    else:
        if price <= tp2:
            return "HIT_TP2"
        if price >= entry:
            return "CLOSED_BE"
    return None


# ---------------------------------------------------------------- the machine

def resolve(sig, price, broker_exit=None):
    """
    sig: signal row (mapping). price: latest price. broker_exit: the broker-confirmed exit label for the
    tracked LIVE leg (from reconcile_exit) or None. Returns a Transition, or None if nothing changed.
    """
    status = sig["status"]
    if status == PENDING:
        return _resolve_leg1(sig, price, broker_exit)
    if status == HIT_TP1:
        return _resolve_runner(sig, price, broker_exit)
    return None


def _resolve_leg1(sig, price, broker_exit):
    d = sig["direction"]
    entry, sl, tp1 = sig["entry_price"], sig["sl_price"], sig["tp1_price"]
    dual = bool(sig["dual_entry"])
    outcome = broker_exit if _live(sig, 1) else _paper_leg1(d, price, sl, tp1)
    runner_live = dual and _live(sig, 2)

    if outcome == "WON_TP1":
        if dual:
            return Transition(
                "HIT_TP1",
                f"🎯 **XAUUSD {d} — LEG 1 (TP1) HIT!** (${tp1:.2f})\n"
                f"🛡️ *Leg 2's Stop Loss is being trailed to Break-Even (${entry:.2f}) — running for TP2.*",
                trail_leg2_to_be=_live(sig, 2) and not sig["be_moved2"])
        return Transition("CLOSED_TP1", f"🎯 **XAUUSD {d} — TP1 HIT! FULL CLOSE.** (${tp1:.2f})")

    if outcome == "HIT_SL":
        return Transition(
            "HIT_SL", f"🛡️ **XAUUSD {d} — STOP LOSS HIT** (${sl:.2f})",
            close_leg2_reason="leg 1 stopped out" if runner_live else None)

    if outcome == "MANUAL":
        return Transition(
            "CLOSED_MANUAL",
            f"🖐️ **XAUUSD {d} — POSITION CLOSED EARLY**\n"
            f"*Closed manually (or by flatten) — not an SL/TP exit.*",
            close_leg2_reason="leg 1 closed manually" if runner_live else None)
    return None


def _resolve_runner(sig, price, broker_exit):
    d = sig["direction"]
    entry, tp2 = sig["entry_price"], sig["tp2_price"]
    outcome = broker_exit if _live(sig, 2) else _paper_runner(d, price, entry, tp2)

    if outcome == "HIT_TP2":
        return Transition("HIT_TP2", f"🚀 **XAUUSD {d} — LEG 2 TP2 HIT!** (${tp2:.2f}) — Full Target Reached!")
    if outcome == "CLOSED_BE":
        return Transition("CLOSED_BE", f"🔒 **XAUUSD {d} — LEG 2 CLOSED AT BREAK-EVEN** (${entry:.2f})")
    if outcome == "MANUAL":
        return Transition("CLOSED_MANUAL", f"🖐️ **XAUUSD {d} — RUNNER CLOSED EARLY** (manual / flatten, not SL/TP)")
    return None