# core/execution/orders.py
"""Everything that sends an order to the broker: open, modify SL/TP, close, trail to break-even."""

import logging

from core.mt5 import mt5

from config import ALERT_STATE
from core.execution.broker import (adjust_levels, filling_modes, forget_filling, remember_filling,
                                   snap_volume)
from core.execution.common import (MAX_SEND_RETRIES, RC_INVALID_FILL, RC_RETRYABLE, comment, deviation,
                                   magic, mark_fill, rc_text)
from core.execution.gates import preflight
from core.execution.positions import fetch_managed, position_by_ticket
from core.mt5 import MT5_LOCK, get_tick
from utils.logkit import trade_event


def _new_fill(symbol, direction, before):
    """A managed position of the right side that wasn't there before we started sending."""
    want = mt5.POSITION_TYPE_BUY if direction == "BUY" else mt5.POSITION_TYPE_SELL
    for p in (fetch_managed(symbol) or []):
        if p.ticket not in before and p.type == want:
            return p
    return None


# ---------------------------------------------------------------- open

def _open_position(symbol, direction, lots, sl, tp, dry_run=None, tag=None, bypass_position_cap=False):
    dry = ALERT_STATE.get("trade_dry_run", True) if dry_run is None else dry_run
    ok, reason = preflight(symbol, bypass_position_cap=bypass_position_cap, dry=dry)
    if not ok:
        return {"ok": False, "dry_run": dry, "ticket": None, "reason": reason}

    with MT5_LOCK:
        info = mt5.symbol_info(symbol)
        if info is not None and not info.visible:
            mt5.symbol_select(symbol, True)
            info = mt5.symbol_info(symbol)
    if info is None:
        return {"ok": False, "dry_run": dry, "ticket": None, "reason": "Symbol info unavailable."}

    lots = snap_volume(info, float(lots))
    if lots <= 0:
        return {"ok": False, "dry_run": dry, "ticket": None, "reason": "Computed lot size is zero."}

    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    last_reason = "Order was never sent."
    before = {p.ticket for p in (fetch_managed(symbol) or [])}

    for attempt in range(1, MAX_SEND_RETRIES + 1):
        tick = get_tick(symbol)
        if tick is None:
            return {"ok": False, "dry_run": dry, "ticket": None, "reason": "No live tick for pricing."}

        if attempt > 1:
            # A previous attempt may have filled even though we got None/timeout/requote back.
            dup = _new_fill(symbol, direction, before)
            if dup is not None:
                mark_fill()
                return {"ok": True, "dry_run": False, "ticket": int(dup.ticket),
                        "price": float(dup.price_open), "lots": float(dup.volume),
                        "sl": dup.sl, "tp": dup.tp, "retcode": mt5.TRADE_RETCODE_DONE,
                        "reason": "Filled (confirmed from position list after ambiguous reply)."}

        price = tick.ask if direction == "BUY" else tick.bid
        adj_sl, adj_tp = adjust_levels(info, direction, price, sl, tp)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lots),
            "type": order_type,
            "price": float(price),
            "sl": float(adj_sl) if adj_sl is not None else 0.0,
            "tp": float(adj_tp) if adj_tp is not None else 0.0,
            "deviation": deviation(),
            "magic": magic(),
            "comment": comment(tag),
            "type_time": mt5.ORDER_TIME_GTC,
        }

        if dry:
            request["type_filling"] = filling_modes(symbol, info)[0]
            logging.info(f"🧪 DRY-RUN order_send: {request}")
            return {"ok": True, "dry_run": True, "ticket": None, "price": price,
                    "lots": lots, "sl": adj_sl, "tp": adj_tp, "request": request,
                    "reason": "Dry-run — no order sent."}

        retry_outer = False
        for filling in filling_modes(symbol, info):
            request["type_filling"] = filling
            with MT5_LOCK:
                result = mt5.order_send(request)

            if result is None:
                with MT5_LOCK:
                    err = mt5.last_error()
                last_reason = f"order_send returned None ({err})."
                retry_outer = True
                break

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                mark_fill()
                remember_filling(symbol, filling)
                ticket = int(result.order)
                # result.order is not reliably the POSITION ticket (e.g. on a netting account adding
                # to an existing position). Resolve the real position id from the fill's own deal
                # record so every downstream lookup (position_by_ticket, reconcile_exit, close)
                # targets the right position.
                deal_id = int(getattr(result, "deal", 0) or 0)
                if deal_id:
                    with MT5_LOCK:
                        deals = mt5.history_deals_get(ticket=deal_id)
                    if deals:
                        ticket = int(deals[0].position_id)
                return {"ok": True, "dry_run": False, "ticket": ticket,
                        "price": float(result.price or price), "lots": float(result.volume or lots),
                        "sl": adj_sl, "tp": adj_tp, "retcode": result.retcode,
                        "reason": "Filled."}

            if result.retcode == RC_INVALID_FILL:
                forget_filling(symbol)
                last_reason = rc_text(result.retcode)
                continue                      # try the next filling policy

            last_reason = f"{rc_text(result.retcode)} — {result.comment}"
            if result.retcode in RC_RETRYABLE:
                retry_outer = True
                break
            return {"ok": False, "dry_run": False, "ticket": None,
                    "retcode": result.retcode, "reason": last_reason}

        if not retry_outer:
            break

    return {"ok": False, "dry_run": dry, "ticket": None,
            "reason": f"{last_reason} (gave up after {MAX_SEND_RETRIES} attempts)"}


def open_position(symbol, direction, lots, sl, tp, dry_run=None, tag=None, bypass_position_cap=False):
    res = _open_position(symbol, direction, lots, sl, tp, dry_run, tag, bypass_position_cap)
    trade_event("order.open", logging.INFO if res["ok"] else logging.WARNING,
                ok=res["ok"], dry=res.get("dry_run"), symbol=symbol, dir=direction,
                lots=res.get("lots", lots), ticket=res.get("ticket"), price=res.get("price"),
                sl=res.get("sl", sl), tp=res.get("tp", tp), tag=tag, reason=res.get("reason"))
    return res


# ---------------------------------------------------------------- modify / close

def _modify_position_sltp(ticket, sl=None, tp=None):
    pos = position_by_ticket(ticket)
    if pos is None:
        return {"ok": False, "reason": "Position not found (already closed?)."}

    with MT5_LOCK:
        info = mt5.symbol_info(pos.symbol)
    tick = get_tick(pos.symbol)
    if info is None or tick is None:
        return {"ok": False, "reason": "Symbol data unavailable."}

    direction = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
    ref_price = tick.bid if direction == "BUY" else tick.ask
    adj_sl, adj_tp = adjust_levels(info, direction, ref_price,
                                   sl if sl is not None else (pos.sl or None),
                                   tp if tp is not None else (pos.tp or None))

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": int(ticket),
        "symbol": pos.symbol,
        "sl": float(adj_sl) if adj_sl else 0.0,
        "tp": float(adj_tp) if adj_tp else 0.0,
        "magic": magic(),
    }
    with MT5_LOCK:
        result = mt5.order_send(request)
        err = mt5.last_error() if result is None else None
    if result is None:
        return {"ok": False, "reason": f"order_send returned None ({err})."}
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        return {"ok": True, "sl": adj_sl, "tp": adj_tp, "reason": "SL/TP updated."}
    if result.retcode == getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025):
        return {"ok": True, "sl": adj_sl, "tp": adj_tp, "reason": "SL/TP already at requested levels."}
    return {"ok": False, "retcode": result.retcode,
            "reason": f"{rc_text(result.retcode)} — {result.comment}"}


def modify_position_sltp(ticket, sl=None, tp=None):
    res = _modify_position_sltp(ticket, sl, tp)
    trade_event("order.modify", logging.INFO if res["ok"] else logging.WARNING,
                ok=res["ok"], ticket=ticket, sl=res.get("sl", sl), tp=res.get("tp", tp), reason=res.get("reason"))
    return res


def _close_position(ticket, lots=None):
    pos = position_by_ticket(ticket)
    if pos is None:
        return {"ok": False, "reason": "Position not found (already closed?)."}
    if pos.magic != magic():
        return {"ok": False, "reason": "Not a bot-managed position (magic mismatch) — refusing."}

    with MT5_LOCK:
        info = mt5.symbol_info(pos.symbol)
    if info is None:
        return {"ok": False, "reason": "Symbol info unavailable."}

    vol = pos.volume if lots is None else float(lots)
    close_vol = snap_volume(info, min(vol, pos.volume))
    remaining = round(pos.volume - close_vol, 2)
    if 0 < remaining < (info.volume_min - 1e-9):
        close_vol = pos.volume          # remainder would be untradeable -> close it all
    if close_vol < info.volume_min - 1e-9:
        return {"ok": False, "reason": f"Cannot close {vol} lots — below volume_min {info.volume_min}."}

    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    order_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
    last_reason = "Close was never sent."

    for _ in range(MAX_SEND_RETRIES):
        tick = get_tick(pos.symbol)
        if tick is None:
            return {"ok": False, "reason": "No live tick for closing price."}
        price = tick.bid if is_buy else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": int(ticket),
            "symbol": pos.symbol,
            "volume": float(close_vol),
            "type": order_type,
            "price": float(price),
            "deviation": deviation(),
            "magic": magic(),
            "comment": "TB close"[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }
        for filling in filling_modes(pos.symbol, info):
            request["type_filling"] = filling
            with MT5_LOCK:
                result = mt5.order_send(request)
            if result is None:
                last_reason = "order_send returned None."
                break
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                remember_filling(pos.symbol, filling)
                return {"ok": True, "closed": close_vol, "price": float(result.price or price),
                        "reason": f"Closed {close_vol} lots."}
            if result.retcode == RC_INVALID_FILL:
                continue
            last_reason = f"{rc_text(result.retcode)} — {result.comment}"
            if result.retcode not in RC_RETRYABLE:
                return {"ok": False, "retcode": result.retcode, "reason": last_reason}
            break

    return {"ok": False, "reason": last_reason}


def close_position(ticket, lots=None):
    res = _close_position(ticket, lots)
    trade_event("order.close", logging.INFO if res["ok"] else logging.WARNING,
                ok=res["ok"], ticket=ticket, lots=res.get("closed", lots), price=res.get("price"),
                reason=res.get("reason"))
    return res


def close_all_managed(symbol=None):
    positions = fetch_managed(symbol)
    if positions is None:
        return ["❌ MT5 position query FAILED — nothing was closed. Retry or close manually in the terminal."]
    out = []
    for pos in positions:
        res = close_position(pos.ticket)
        out.append(f"#{pos.ticket} {pos.volume} lots — {'✅' if res['ok'] else '❌'} {res['reason']}")
    return out or ["No managed positions open."]


# ---------------------------------------------------------------- leg management

def trail_leg_to_breakeven(ticket, entry_price):
    pos = position_by_ticket(ticket)
    if pos is None:
        return True, "Leg already closed by the broker — nothing to trail."
    res = modify_position_sltp(ticket, sl=float(entry_price), tp=pos.tp or None)
    return res["ok"], f"SL → BE: {'✅' if res['ok'] else '⚠️'} {res['reason']}"


def force_close_leg(ticket, reason_label="invalidated"):
    """Closes a leg immediately at market — used when its sibling leg just hit
    SL, so the whole trade idea is invalidated and the runner shouldn't be left
    dangling on its own."""
    pos = position_by_ticket(ticket)
    if pos is None:
        return f"Leg already closed — nothing to do ({reason_label})."
    res = close_position(ticket)
    return f"Force-close ({reason_label}): {'✅' if res['ok'] else '⚠️'} {res['reason']}"