# core/execution/drain.py
"""Turns queued trade ideas into orders. Everything here is BLOCKING — call via asyncio.to_thread()."""

from config import ALERT_STATE
from core.database import attach_trade_execution, set_signal_status
from core.execution.common import EXEC_LOCK, fixed_lot
from core.execution.gates import trade_slot_blocker
from core.execution.intents import take_pending
from core.execution.orders import open_position
from core.mt5 import get_tick


def execute_pending_intents():
    """BLOCKING. Drains the queue, sends orders, returns Telegram-ready report lines."""
    batch = take_pending()
    if not batch:
        return []
    with EXEC_LOCK:
        return _drain_batch(batch)


def _skip_intent(intent, dual):
    """Records that no order was sent and takes the signal out of outcome tracking."""
    lot = fixed_lot()
    attach_trade_execution(intent["signal_id"], 1, None, lot, "NONE", None)
    if dual:
        attach_trade_execution(intent["signal_id"], 2, None, lot, "NONE", None)
    set_signal_status(intent["signal_id"], "SKIPPED")


def _drain_batch(batch):
    reports = []
    default_lot = fixed_lot()
    handled_symbols = set()   # only ONE trade idea per symbol per drain, even in dry-run

    for intent in batch:
        symbol, direction, dual = intent["symbol"], intent["direction"], intent["dual_entry"]
        leg_lots = float(intent.get("lots") or default_lot)
        sl_dist = abs(intent["entry"] - intent["sl"])
        if sl_dist <= 1e-9:
            _skip_intent(intent, dual)
            reports.append(f"❌ **Execution skipped** — zero SL distance on {direction} {symbol}.")
            continue

        if symbol in handled_symbols:
            blocker = "another signal on this symbol was already handled this cycle."
        else:
            blocker = trade_slot_blocker(symbol, exclude_signal_id=intent["signal_id"])
        if blocker:
            _skip_intent(intent, dual)
            reports.append(f"⏭️ **Execution skipped** — {direction} {symbol} signal ignored, {blocker}")
            continue

        tick = get_tick(symbol)
        if tick is not None:
            live = tick.ask if direction == "BUY" else tick.bid
            drift_pct = abs(live - intent["entry"]) / sl_dist * 100.0
            max_drift = float(ALERT_STATE.get("max_entry_drift_pct", 25.0))
            if drift_pct > max_drift:
                _skip_intent(intent, dual)
                reports.append(
                    f"🚫 **Execution aborted** — {direction} {symbol} drifted "
                    f"`{drift_pct:.0f}%` of SL distance from the signal price "
                    f"(limit `{max_drift:.0f}%`)."
                )
                continue

        handled_symbols.add(symbol)

        legs = [(1, "TP1", intent["tp1"], False)]
        if dual:
            legs.append((2, "TP2", intent["tp2"], True))  # only leg 2 bypasses the gate

        leg_lines = []
        leg1_ok = False
        for leg_no, label, tp_price, bypass_cap in legs:
            if leg_no == 2 and not leg1_ok:
                attach_trade_execution(intent["signal_id"], 2, None, leg_lots, "NONE", None)
                leg_lines.append("⏭️ Leg 2 (TP2) skipped — leg 1 did not open.")
                continue

            res = open_position(symbol, direction, leg_lots, intent["sl"], tp_price,
                                tag=f"TB-{label}", bypass_position_cap=bypass_cap)

            if leg_no == 1:
                leg1_ok = res["ok"]

            if res["ok"] and res.get("dry_run"):
                attach_trade_execution(intent["signal_id"], leg_no, None, leg_lots, "DRY", res.get("price"))
                leg_lines.append(
                    f"🧪 Leg {leg_no} ({label}) DRY-RUN — `{leg_lots}` lots @ ~`${res['price']:.2f}`, "
                    f"SL `${res['sl']}`, TP `${res['tp']}`"
                )
            elif res["ok"]:
                attach_trade_execution(intent["signal_id"], leg_no, res["ticket"], res["lots"], "LIVE", res.get("price"))
                leg_lines.append(
                    f"✅ Leg {leg_no} ({label}) FILLED `#{res['ticket']}` — `{res['lots']}` lots @ `${res['price']:.2f}`, "
                    f"SL `${res['sl']}`, TP `${res['tp']}`"
                )
            else:
                attach_trade_execution(intent["signal_id"], leg_no, None, leg_lots, "NONE", None)
                leg_lines.append(f"❌ Leg {leg_no} ({label}) failed — {res['reason']}")

        if not leg1_ok:
            set_signal_status(intent["signal_id"], "SKIPPED")

        mode_label = "🎯🎯 DUAL-ENTRY" if dual else "🎯 SINGLE-ENTRY (TP1-only)"
        header = f"{mode_label} — `{direction} {symbol}` (score `{intent['score']}`)"
        reports.append(header + "\n" + "\n".join(f"• {line}" for line in leg_lines))

    return reports