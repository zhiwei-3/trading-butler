# core/execution/gates.py
"""The one-trade-at-a-time gate and the pre-send checks. FAIL CLOSED: when MT5 or the DB can't be
queried, no new trade opens."""

from core.mt5 import mt5

from config import ALERT_STATE
from core.db import count_live_trades_today, get_open_executed_signals
from core.execution.common import FILL_COOLDOWN_S, seconds_since_fill
from core.execution.intents import pending_intent_count
from core.execution.positions import fetch_managed
from core.mt5 import MT5_LOCK, init_mt5


def trade_slot_blocker(symbol, exclude_signal_id=None):
    """One-trade-at-a-time gate. FAILS CLOSED. Returns None if a new idea may open.
    exclude_signal_id: the signal being evaluated (so it never blocks itself)."""
    if seconds_since_fill() < FILL_COOLDOWN_S:
        return "a trade was just opened — waiting for the position list to settle."

    if pending_intent_count() > 0 and exclude_signal_id is None:
        return "another signal is already queued for execution."

    # Dry-run has no real MT5 positions, so the DB is the source of truth for
    # "is a simulated trade still open?".
    if ALERT_STATE.get("trade_dry_run", True):
        try:
            open_sigs = get_open_executed_signals(exclude_id=exclude_signal_id)
        except Exception as e:
            return f"Could not verify simulated open trades ({e}) — failing closed."
        if open_sigs:
            ids = ", ".join(f"#{s['id']}" for s in open_sigs)
            return f"already holding simulated position from signal(s) {ids}."

    if not init_mt5():
        return "MT5 terminal unreachable — cannot verify open positions."
    managed = fetch_managed(symbol)
    if managed is None:
        return "Could not verify open positions (MT5 query failed) — failing closed."
    if managed:
        return f"already holding position(s) {', '.join(f'#{p.ticket}' for p in managed)}."
    return None


def preflight(symbol, bypass_position_cap=False, dry=None):
    dry = ALERT_STATE.get("trade_dry_run", True) if dry is None else dry
    if not init_mt5():
        return False, "MT5 terminal unreachable."
    with MT5_LOCK:
        term = mt5.terminal_info()
        acc = mt5.account_info()
        info = mt5.symbol_info(symbol)
    if info is None:
        return False, f"Symbol `{symbol}` not found on this server."

    if not dry:   # permissions only matter when a real order will be sent
        if term is None or not term.trade_allowed:
            return False, "AutoTrading is OFF in the MT5 terminal (enable the 'Algo Trading' button)."
        if acc is None:
            return False, "No MT5 account info available."
        if not acc.trade_allowed:
            return False, "Trading disabled for this account (investor password or server restriction)."
        if info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
            return False, f"`{symbol}` is not open for full trading (trade_mode={info.trade_mode})."

    if not bypass_position_cap:
        blocker = trade_slot_blocker(symbol)
        if blocker:
            return False, blocker.capitalize()

    day_n = count_live_trades_today()
    day_cap = int(ALERT_STATE.get("max_daily_trades", 5))
    if not dry and day_n >= day_cap:
        return False, f"Daily execution cap reached ({day_n}/{day_cap})."
    return True, ""