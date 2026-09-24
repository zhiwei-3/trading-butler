# trade_engine.py
"""
Live MT5 order execution for Trading Butler.

Boundaries:
  * mt5_engine.py  -> terminal connectivity + broker helpers
  * trade_engine.py -> order_send / modify / close, position management
  * strategy/       -> decides WHAT to trade (never sends orders itself)
  * bot/jobs.py     -> drains execution intents and manages leg outcomes off the event loop

Every function here is BLOCKING and must be called via asyncio.to_thread()
from async code. All MT5 access goes through MT5_LOCK (the MT5 module is not
thread-safe and will hang silently on concurrent calls).

=== Two-leg execution model ===
Every executed signal gets a "leg 1" position: fixed lot size, broker TP = TP1,
broker SL = the signal's SL. It always runs to a full close at either TP1 or SL
— there is no partial close on this leg.

A signal whose strategy is smc_confluence AND whose confluence score is >=
ALERT_STATE["dual_entry_score_threshold"] ALSO gets a "leg 2" position: same
fixed lot size, broker TP = TP2, broker SL = the same original SL. The moment
leg 1 closes profitably at TP1, leg 2's SL is trailed to break-even so it can
only resolve at break-even or TP2. If leg 1 instead gets stopped at SL, leg 2
(which shares the same SL) is force-closed immediately rather than left to
possibly resolve inconsistently.

All strategies other than smc_confluence, and any smc_confluence signal below
the dual-entry threshold, get leg 1 only (single position, TP1-only, no runner).
"""

import math
import logging
import threading
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5

from config import ALERT_STATE
from database import attach_trade_execution, count_live_trades_today
from mt5_engine import MT5_LOCK, init_mt5, get_tick

MAX_SEND_RETRIES = 3

_INTENT_LOCK = threading.Lock()
_PENDING_INTENTS = []
_FILLING_CACHE = {}   # symbol -> filling mode that the broker actually accepted

_RC_INVALID_FILL = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)
_RC_RETRYABLE = {
    getattr(mt5, "TRADE_RETCODE_REQUOTE", 10004),
    getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", 10020),
    getattr(mt5, "TRADE_RETCODE_PRICE_OFF", 10021),
    getattr(mt5, "TRADE_RETCODE_CONNECTION", 10031),
}
_RC_TEXT = {
    10004: "Requote",
    10006: "Request rejected by broker",
    10013: "Invalid request",
    10014: "Invalid volume",
    10015: "Invalid price",
    10016: "Invalid stops (SL/TP too close or wrong side)",
    10018: "Market is closed",
    10019: "Insufficient free margin",
    10027: "AutoTrading disabled in terminal",
    10028: "AutoTrading disabled by server",
    10030: "Unsupported filling mode",
    10031: "No connection to trade server",
}

def _rc_text(rc):
    return _RC_TEXT.get(rc, f"retcode {rc}")

def _magic():
    return int(ALERT_STATE.get("magic_number", 770077))

def _comment(tag=None):
    return str(tag or ALERT_STATE.get("trade_comment", "TradingButler"))[:31]

def _deviation():
    return int(ALERT_STATE.get("max_slippage_points", 30))

def _fixed_lot():
    return float(ALERT_STATE.get("fixed_lot_size", 0.01))


# ---------------------------------------------------------------- intents

def request_execution(signal_id, symbol, direction, entry, sl_price, tp1_price, tp2_price, score, dual_entry=False):
    """Called by the strategy layer. Queues an intent; never touches MT5 itself."""
    if not ALERT_STATE.get("auto_trade_enabled", False):
        return False
    with _INTENT_LOCK:
        _PENDING_INTENTS.append({
            "signal_id": signal_id, "symbol": symbol, "direction": direction,
            "entry": float(entry), "sl": float(sl_price),
            "tp1": float(tp1_price), "tp2": float(tp2_price),
            "score": score, "dual_entry": bool(dual_entry),
            "queued_at": datetime.now(timezone.utc),
        })
    return True

def pending_intent_count():
    with _INTENT_LOCK:
        return len(_PENDING_INTENTS)


# ---------------------------------------------------------------- preflight

def list_managed_positions(symbol=None):
    """Only positions carrying this bot's magic number — manual trades are untouchable."""
    with MT5_LOCK:
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    return [p for p in (positions or []) if p.magic == _magic()]

def position_by_ticket(ticket):
    with MT5_LOCK:
        positions = mt5.positions_get(ticket=int(ticket))
    return positions[0] if positions else None

def _preflight(symbol, bypass_position_cap=False):
    if not init_mt5():
        return False, "MT5 terminal unreachable."
    with MT5_LOCK:
        term = mt5.terminal_info()
        acc = mt5.account_info()
        info = mt5.symbol_info(symbol)
    if term is None or not term.trade_allowed:
        return False, "AutoTrading is OFF in the MT5 terminal (enable the 'Algo Trading' button)."
    if acc is None:
        return False, "No MT5 account info available."
    if not acc.trade_allowed:
        return False, "Trading disabled for this account (investor password or server restriction)."
    if info is None:
        return False, f"Symbol `{symbol}` not found on this server."
    if info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
        return False, f"`{symbol}` is not open for full trading (trade_mode={info.trade_mode})."
    # bypass_position_cap: the 2nd leg of a dual-entry signal is part of the SAME
    # trade idea as the 1st leg, not a new one — the cap is meant to stop
    # unrelated signals stacking up, not to block a signal's own paired leg.
    if not bypass_position_cap:
        open_n = len(list_managed_positions(symbol))
        cap = int(ALERT_STATE.get("max_open_positions", 2))
        if open_n >= cap:
            return False, f"Already holding {open_n} managed position(s) — cap is {cap}."
    day_n = count_live_trades_today()
    day_cap = int(ALERT_STATE.get("max_daily_trades", 5))
    if not ALERT_STATE.get("trade_dry_run", True) and day_n >= day_cap:
        return False, f"Daily execution cap reached ({day_n}/{day_cap})."
    return True, ""


# ---------------------------------------------------------------- broker quirks

def _filling_modes(symbol, info):
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

def _adjust_levels(info, direction, price, sl, tp):
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

def _snap_volume(info, volume):
    step = info.volume_step or 0.01
    vol = math.floor((volume / step) + 1e-9) * step
    return round(max(info.volume_min, min(info.volume_max, vol)), 2)


# ---------------------------------------------------------------- open

def open_position(symbol, direction, lots, sl, tp, dry_run=None, tag=None, bypass_position_cap=False):
    dry = ALERT_STATE.get("trade_dry_run", True) if dry_run is None else dry_run
    ok, reason = _preflight(symbol, bypass_position_cap=bypass_position_cap)
    if not ok:
        return {"ok": False, "dry_run": dry, "ticket": None, "reason": reason}

    with MT5_LOCK:
        info = mt5.symbol_info(symbol)
        if info is not None and not info.visible:
            mt5.symbol_select(symbol, True)
            info = mt5.symbol_info(symbol)
    if info is None:
        return {"ok": False, "dry_run": dry, "ticket": None, "reason": "Symbol info unavailable."}

    lots = _snap_volume(info, float(lots))
    if lots <= 0:
        return {"ok": False, "dry_run": dry, "ticket": None, "reason": "Computed lot size is zero."}

    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    last_reason = "Order was never sent."

    for attempt in range(1, MAX_SEND_RETRIES + 1):
        tick = get_tick(symbol)
        if tick is None:
            return {"ok": False, "dry_run": dry, "ticket": None, "reason": "No live tick for pricing."}

        price = tick.ask if direction == "BUY" else tick.bid
        adj_sl, adj_tp = _adjust_levels(info, direction, price, sl, tp)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lots),
            "type": order_type,
            "price": float(price),
            "sl": float(adj_sl) if adj_sl is not None else 0.0,
            "tp": float(adj_tp) if adj_tp is not None else 0.0,
            "deviation": _deviation(),
            "magic": _magic(),
            "comment": _comment(tag),
            "type_time": mt5.ORDER_TIME_GTC,
        }

        if dry:
            request["type_filling"] = _filling_modes(symbol, info)[0]
            logging.info(f"🧪 DRY-RUN order_send: {request}")
            return {"ok": True, "dry_run": True, "ticket": None, "price": price,
                    "lots": lots, "sl": adj_sl, "tp": adj_tp, "request": request,
                    "reason": "Dry-run — no order sent."}

        retry_outer = False
        for filling in _filling_modes(symbol, info):
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
                _FILLING_CACHE[symbol] = filling
                ticket = int(result.order)
                # BUGFIX: result.order is not reliably the POSITION ticket (e.g. on
                # a netting account adding to an existing position). Resolve the
                # real position id from the fill's own deal record so every
                # downstream lookup (position_by_ticket, reconcile_exit, close)
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

            if result.retcode == _RC_INVALID_FILL:
                _FILLING_CACHE.pop(symbol, None)
                last_reason = _rc_text(result.retcode)
                continue                      # try the next filling policy

            last_reason = f"{_rc_text(result.retcode)} — {result.comment}"
            if result.retcode in _RC_RETRYABLE:
                retry_outer = True
                break
            return {"ok": False, "dry_run": False, "ticket": None,
                    "retcode": result.retcode, "reason": last_reason}

        if not retry_outer:
            break

    return {"ok": False, "dry_run": dry, "ticket": None,
            "reason": f"{last_reason} (gave up after {MAX_SEND_RETRIES} attempts)"}


# ---------------------------------------------------------------- modify / close

def modify_position_sltp(ticket, sl=None, tp=None):
    pos = position_by_ticket(ticket)
    if pos is None:
        return {"ok": False, "reason": "Position not found (already closed?)."}
    if ALERT_STATE.get("trade_dry_run", True):
        logging.info(f"🧪 DRY-RUN SLTP #{ticket} -> sl={sl} tp={tp}")
        return {"ok": True, "dry_run": True, "reason": "Dry-run — SL/TP not modified."}

    with MT5_LOCK:
        info = mt5.symbol_info(pos.symbol)
    tick = get_tick(pos.symbol)
    if info is None or tick is None:
        return {"ok": False, "reason": "Symbol data unavailable."}

    direction = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
    ref_price = tick.bid if direction == "BUY" else tick.ask
    adj_sl, adj_tp = _adjust_levels(info, direction, ref_price,
                                    sl if sl is not None else pos.sl or None,
                                    tp if tp is not None else pos.tp or None)

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": int(ticket),
        "symbol": pos.symbol,
        "sl": float(adj_sl) if adj_sl else 0.0,
        "tp": float(adj_tp) if adj_tp else 0.0,
        "magic": _magic(),
    }
    with MT5_LOCK:
        result = mt5.order_send(request)
    if result is None:
        return {"ok": False, "reason": "order_send returned None."}
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        return {"ok": True, "sl": adj_sl, "tp": adj_tp, "reason": "SL/TP updated."}
    return {"ok": False, "retcode": result.retcode,
            "reason": f"{_rc_text(result.retcode)} — {result.comment}"}

def close_position(ticket, lots=None):
    """Full or partial close. Refuses a partial that would leave a sub-minimum remainder."""
    pos = position_by_ticket(ticket)
    if pos is None:
        return {"ok": False, "reason": "Position not found (already closed?)."}

    with MT5_LOCK:
        info = mt5.symbol_info(pos.symbol)
    if info is None:
        return {"ok": False, "reason": "Symbol info unavailable."}

    vol = pos.volume if lots is None else float(lots)
    close_vol = _snap_volume(info, min(vol, pos.volume))
    remaining = round(pos.volume - close_vol, 2)
    if 0 < remaining < (info.volume_min - 1e-9):
        close_vol = pos.volume          # remainder would be untradeable -> close it all
    if close_vol < info.volume_min - 1e-9:
        return {"ok": False, "reason": f"Cannot close {vol} lots — below volume_min {info.volume_min}."}

    if ALERT_STATE.get("trade_dry_run", True):
        logging.info(f"🧪 DRY-RUN close #{ticket} vol={close_vol}")
        return {"ok": True, "dry_run": True, "closed": close_vol, "reason": "Dry-run — nothing closed."}

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
            "deviation": _deviation(),
            "magic": _magic(),
            "comment": "TB close"[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }
        for filling in _filling_modes(pos.symbol, info):
            request["type_filling"] = filling
            with MT5_LOCK:
                result = mt5.order_send(request)
            if result is None:
                last_reason = "order_send returned None."
                break
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                _FILLING_CACHE[pos.symbol] = filling
                return {"ok": True, "closed": close_vol, "price": float(result.price or price),
                        "reason": f"Closed {close_vol} lots."}
            if result.retcode == _RC_INVALID_FILL:
                continue
            last_reason = f"{_rc_text(result.retcode)} — {result.comment}"
            if result.retcode not in _RC_RETRYABLE:
                return {"ok": False, "retcode": result.retcode, "reason": last_reason}
            break

    return {"ok": False, "reason": last_reason}

def close_all_managed(symbol=None):
    """Kill switch. Returns one result line per managed position."""
    out = []
    for pos in list_managed_positions(symbol):
        res = close_position(pos.ticket)
        out.append(f"#{pos.ticket} {pos.volume} lots — {'✅' if res['ok'] else '❌'} {res['reason']}")
    return out or ["No managed positions open."]


# ---------------------------------------------------------------- leg management

def trail_leg_to_breakeven(ticket, entry_price):
    """Moves a still-open leg's SL to its entry price (used on the TP2 runner
    once the TP1 leg has won)."""
    pos = position_by_ticket(ticket)
    if pos is None:
        return "Leg already closed by the broker — nothing to trail."
    res = modify_position_sltp(ticket, sl=float(entry_price), tp=pos.tp or None)
    return f"SL → BE: {'✅' if res['ok'] else '⚠️'} {res['reason']}"

def force_close_leg(ticket, reason_label="invalidated"):
    """Closes a leg immediately at market — used when its sibling leg just hit
    SL, so the whole trade idea is invalidated and the runner shouldn't be left
    dangling on its own."""
    pos = position_by_ticket(ticket)
    if pos is None:
        return f"Leg already closed — nothing to do ({reason_label})."
    res = close_position(ticket)
    return f"Force-close ({reason_label}): {'✅' if res['ok'] else '⚠️'} {res['reason']}"


# ---------------------------------------------------------------- reconciliation

def reconcile_exit(ticket, candidates):
    """
    If `ticket`'s position is closed, classify the outcome by which known price
    level (candidates: {label: price}) the FINAL exit deal landed closest to.

    BUGFIX (vs. an earlier profit-sum approach): summing profit across every
    deal on a ticket misclassifies a leg whenever more than one exit deal exists
    on it (e.g. a broker-side partial fill quirk). Comparing the *last* exit
    deal's actual price against the known SL/TP/BE levels is robust regardless
    of how many deals make up the position's history.

    Returns the matching label, or None if the position is still open or no
    deal history is available yet.
    """
    if position_by_ticket(ticket) is not None:
        return None
    try:
        with MT5_LOCK:
            deals = mt5.history_deals_get(position=int(ticket))
        if not deals:
            with MT5_LOCK:
                deals = mt5.history_deals_get(
                    datetime.now(timezone.utc) - timedelta(days=7),
                    datetime.now(timezone.utc) + timedelta(minutes=5),
                    position=int(ticket),
                )
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
    return min(scored, key=lambda k: abs(exit_price - scored[k]))


# ---------------------------------------------------------------- intent drain

def execute_pending_intents():
    """BLOCKING. Drains the queue, sends orders, returns Telegram-ready report lines."""
    with _INTENT_LOCK:
        batch = list(_PENDING_INTENTS)
        _PENDING_INTENTS[:] = []
    if not batch:
        return []

    reports = []
    fixed_lot = _fixed_lot()

    for intent in batch:
        symbol, direction, dual = intent["symbol"], intent["direction"], intent["dual_entry"]
        sl_dist = abs(intent["entry"] - intent["sl"])
        if sl_dist <= 1e-9:  # Use tolerance for floating point
            reports.append(f"❌ **Execution skipped** — zero SL distance on {direction} {symbol}.")
            continue

        # Abort if the market ran away from the signal price while we were queueing.
        tick = get_tick(symbol)
        if tick is not None:
            live = tick.ask if direction == "BUY" else tick.bid
            drift_pct = abs(live - intent["entry"]) / sl_dist * 100.0
            max_drift = float(ALERT_STATE.get("max_entry_drift_pct", 25.0))
            if drift_pct > max_drift:
                reports.append(
                    f"🚫 **Execution aborted** — {direction} {symbol} drifted "
                    f"`{drift_pct:.0f}%` of SL distance from the signal price "
                    f"(limit `{max_drift:.0f}%`)."
                )
                continue

        legs = [(1, "TP1", intent["tp1"], False)]
        if dual:
            legs.append((2, "TP2", intent["tp2"], True))  # leg 2 bypasses the open-position cap

        leg_lines = []
        for leg_no, label, tp_price, bypass_cap in legs:
            res = open_position(symbol, direction, fixed_lot, intent["sl"], tp_price,
                                 tag=f"TB-{label}", bypass_position_cap=bypass_cap)

            if res["ok"] and res.get("dry_run"):
                attach_trade_execution(intent["signal_id"], leg_no, None, fixed_lot, "DRY", res.get("price"))
                leg_lines.append(
                    f"🧪 Leg {leg_no} ({label}) DRY-RUN — `{fixed_lot}` lots @ ~`${res['price']:.2f}`, "
                    f"SL `${res['sl']}`, TP `${res['tp']}`"
                )
            elif res["ok"]:
                attach_trade_execution(intent["signal_id"], leg_no, res["ticket"], res["lots"], "LIVE", res.get("price"))
                leg_lines.append(
                    f"✅ Leg {leg_no} ({label}) FILLED `#{res['ticket']}` — `{res['lots']}` lots @ `${res['price']:.2f}`, "
                    f"SL `${res['sl']}`, TP `${res['tp']}`"
                )
            else:
                attach_trade_execution(intent["signal_id"], leg_no, None, fixed_lot, "NONE", None)
                leg_lines.append(f"❌ Leg {leg_no} ({label}) failed — {res['reason']}")

        mode_label = "🎯🎯 DUAL-ENTRY" if dual else "🎯 SINGLE-ENTRY (TP1-only)"
        header = f"{mode_label} — `{direction} {symbol}` (score `{intent['score']}`)"
        reports.append(header + "\n" + "\n".join(f"• {line}" for line in leg_lines))

    return reports
