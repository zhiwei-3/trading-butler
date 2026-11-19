# core/execution/pnl.py
"""Telegram-ready P&L blocks for the legs a status transition closed."""

import MetaTrader5 as mt5

from core.execution.common import fixed_lot
from core.execution.reconcile import position_realized_pnl
from core.mt5_engine import MT5_LOCK


def _account_currency():
    try:
        with MT5_LOCK:
            acc = mt5.account_info()
        if acc is not None and acc.currency:
            return acc.currency
    except Exception:
        pass
    return "USD"


def _sim_leg_pnl(symbol, direction, lots, entry, exit_price):
    size = 100.0
    try:
        with MT5_LOCK:
            info = mt5.symbol_info(symbol)
        if info and info.trade_contract_size:
            size = float(info.trade_contract_size)
    except Exception:
        pass
    diff = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
    return round(diff * float(lots) * size, 2)


def _manual_close_pnl(sig):
    """Real realised P&L of the legs that were closed outside SL/TP."""
    curr = _account_currency()
    lines, total = [], 0.0
    for leg_no, k in ((1, ""), (2, "2")):
        ticket = sig.get("ticket" + k)
        if sig.get("exec_mode" + k) != "LIVE" or not ticket:
            continue
        pnl = position_realized_pnl(ticket)
        if pnl is None:
            continue
        total += pnl
        icon = "✅" if pnl > 0.005 else ("❌" if pnl < -0.005 else "🔒")
        lines.append(f"• Leg {leg_no} {icon}: `{pnl:+,.2f} {curr}`")
    if not lines:
        return ""
    if len(lines) > 1:
        lines.append(f"• **Trade net:** `{total:+,.2f} {curr}`")
    return "💰 **Trade P&L (manual close)**\n" + "\n".join(lines)


def summarize_outcome_pnl(sig, new_status):
    """
    BLOCKING. Telegram-ready P&L block for the legs a status transition closed
    (and, on terminal statuses of a dual trade, the whole-trade total).
      LIVE leg -> real MT5 deal history (falls back to an estimate if history lags)
      DRY leg  -> simulated at the signal levels
      NONE     -> paper (fixed lot at the signal levels)
    """
    sig = dict(sig)

    if new_status == "CLOSED_MANUAL":
        return _manual_close_pnl(sig)

    direction = sig["direction"]
    symbol = sig.get("symbol") or "XAUUSD"
    entry, sl, tp1, tp2 = (float(sig[k]) for k in ("entry_price", "sl_price", "tp1_price", "tp2_price"))
    dual = bool(sig.get("dual_entry"))
    sl_dist = abs(entry - sl)

    if dual:
        plan = {
            "HIT_TP1":   [(1, tp1, "TP1")],
            "HIT_SL":    [(1, sl, "SL"), (2, sl, "SL")],
            "HIT_TP2":   [(1, tp1, "TP1"), (2, tp2, "TP2")],
            "CLOSED_BE": [(1, tp1, "TP1"), (2, entry, "BE")],
        }
    else:
        plan = {"CLOSED_TP1": [(1, tp1, "TP1")], "HIT_SL": [(1, sl, "SL")]}
    legs = plan.get(new_status)
    if not legs:
        return ""
    terminal = new_status != "HIT_TP1"

    curr = _account_currency()
    fmt = lambda v: f"{v:+,.2f} {curr}"
    tags = {"real": "", "est": " (est.)", "sim": " 🧪", "paper": " 📝"}
    lines, total_pnl, total_r = [], 0.0, 0.0

    for leg_no, exit_p, label in legs:
        k = "" if leg_no == 1 else "2"
        mode = sig.get("exec_mode" + k) or "NONE"
        ticket = sig.get("ticket" + k)
        lots = float(sig.get("lots" + k) or fixed_lot())
        fill = float(sig.get("fill_price" + k) or entry)

        pnl, src = None, "real"
        if mode == "LIVE" and ticket:
            pnl = position_realized_pnl(ticket)
        if pnl is None:
            pnl = _sim_leg_pnl(symbol, direction, lots, fill, exit_p)
            src = "est" if mode == "LIVE" else ("sim" if mode == "DRY" else "paper")

        r = ((exit_p - entry) if direction == "BUY" else (entry - exit_p)) / sl_dist if sl_dist > 0 else 0.0
        icon = "✅" if pnl > 0.005 else ("❌" if pnl < -0.005 else "🔒")
        lines.append(f"• Leg {leg_no} ({label}) {icon}: `{fmt(pnl)}` · `{r:+.2f}R`{tags[src]}")
        total_pnl += pnl
        total_r += r

    out = ["💰 **Trade P&L**"] + lines
    if terminal and len(legs) > 1:
        legs_total = 2 if dual else 1
        out.append(f"• **Trade net:** `{fmt(total_pnl)}` · `{total_r / legs_total:+.2f}R`")
    if new_status == "HIT_TP1":
        out.append("• _Runner still open (SL at break-even)._")
    return "\n".join(out)