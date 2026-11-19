# core/execution/__init__.py
"""
Live MT5 order execution for Trading Butler.

Boundaries:
  * core.mt5   terminal connectivity + broker helpers
  * core.execution    order_send / modify / close, position management (this package)
  * strategies/       decide WHAT to trade (never send orders)
  * core.outcomes     decides what happened to a trade (pure)
  * bot/jobs          drains execution intents and manages leg outcomes off the event loop

Every function here is BLOCKING and must be called via asyncio.to_thread() from async code.
All MT5 access goes through MT5_LOCK (the MT5 module is not thread-safe and will hang silently
on concurrent calls).

=== Two-leg execution model ===
Every executed signal gets a "leg 1" position: broker TP = TP1, broker SL = the signal's SL. It
always runs to a full close at either TP1 or SL — there is no partial close on this leg.

A signal whose strategy says `is_dual(...)` ALSO gets a "leg 2" position: same lot size, broker
TP = TP2, broker SL = the same original SL. The moment leg 1 closes profitably at TP1, leg 2's SL is
trailed to break-even so it can only resolve at break-even or TP2. If leg 1 instead gets stopped at
SL, leg 2 (which shares the same SL) is force-closed immediately rather than left to possibly
resolve inconsistently. Single-leg signals get leg 1 only (TP1-only, no runner).

Modules:  common · positions · sizing · intents · gates · broker · orders · reconcile · pnl · drain · enforcer
"""

from core.execution.common import MAX_SEND_RETRIES
from core.execution.positions import list_managed_positions, position_by_ticket
from core.execution.sizing import calc_risk_lots
from core.execution.intents import request_execution, pending_intent_count
from core.execution.gates import trade_slot_blocker
from core.execution.orders import (
    open_position, modify_position_sltp, close_position, close_all_managed,
    trail_leg_to_breakeven, force_close_leg,
)
from core.execution.reconcile import reconcile_exit, position_realized_pnl
from core.execution.pnl import summarize_outcome_pnl
from core.execution.drain import execute_pending_intents
from core.execution.enforcer import enforce_single_trade

__all__ = [
    "MAX_SEND_RETRIES", "list_managed_positions", "position_by_ticket", "calc_risk_lots",
    "request_execution", "pending_intent_count", "trade_slot_blocker",
    "open_position", "modify_position_sltp", "close_position", "close_all_managed",
    "trail_leg_to_breakeven", "force_close_leg", "reconcile_exit", "position_realized_pnl",
    "summarize_outcome_pnl", "execute_pending_intents", "enforce_single_trade",
]