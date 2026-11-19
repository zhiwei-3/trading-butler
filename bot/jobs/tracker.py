# bot/jobs/tracker.py
"""
Outcome tracker: observes the broker / latest price, asks core.outcomes what changed for each tracked
signal, then acts (trail leg 2 to break-even, force-close a runner) and announces the result.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from telegram.ext import ContextTypes

from config import ALERT_STATE, USER_ID
from bot.registry import job
from bot.messaging import send_text, send_alert
from bot.jobs.circuit_breaker import evaluate_circuit_breaker
from core import outcomes
from core.database import (
    get_tracked_signals, update_signal_statuses, mark_leg2_be_moved, get_recent_trades_for_chart,
)
from core.mt5 import get_gold_symbol, fetch_candles
from core.execution import (
    reconcile_exit, trail_leg_to_breakeven, force_close_leg, enforce_single_trade, summarize_outcome_pnl,
)
from utils.chart import generate_outcome_chart


def _zone_from_sig(sig):
    """Rebuilds the S/R zone that existed when the signal fired (None for pre-migration rows)."""
    if sig["zone_low"] is None or sig["zone_high"] is None:
        return None
    low, high = float(sig["zone_low"]), float(sig["zone_high"])
    return {"type": sig["zone_type"] or "mixed", "low": low, "high": high, "price": (low + high) / 2}


# ---------------------------------------------------------------- observe

async def _broker_exit(sig):
    """The broker-confirmed exit label for the tracked live leg, or None (paper legs / not closed yet)."""
    req = outcomes.reconcile_request(sig)
    if req is None:
        return None
    ticket, candidates = req
    try:
        return await asyncio.to_thread(reconcile_exit, ticket, candidates)
    except Exception as e:
        logging.warning(f"Reconcile failed for #{ticket}: {e}")
        return None


# ---------------------------------------------------------------- act

async def _trail_to_be(sig):
    """Returns (ok, note)."""
    try:
        return await asyncio.to_thread(trail_leg_to_breakeven, sig["ticket2"], sig["entry_price"])
    except Exception as e:
        logging.exception(f"Leg2 BE-trail failed for ticket {sig['ticket2']}: {e}")
        return False, f"SL → BE: ⚠️ error ({e})"


async def _close_runner(sig, reason):
    try:
        return await asyncio.to_thread(force_close_leg, sig["ticket2"], reason)
    except Exception as e:
        logging.exception(f"Leg2 force-close failed for ticket {sig['ticket2']}: {e}")
        return f"Leg 2 force-close: ⚠️ error ({e})"


# ---------------------------------------------------------------- announce

async def _announce(context, chat_id, symbol, sig, new_status, message, extra_note, closed_at, df_chart, trades_hist):
    if not (chat_id and message):
        return
    full_msg = message

    # Outcome P&L (real MT5 deals for live legs, simulated for dry-run/paper).
    try:
        pnl_block = await asyncio.to_thread(summarize_outcome_pnl, sig, new_status)
        if pnl_block:
            full_msg += f"\n\n{pnl_block}"
    except Exception:
        logging.exception(f"Outcome P&L failed for signal {sig['id']}")

    # Cooldown notice on stop-outs.
    if new_status == "HIT_SL" and ALERT_STATE.get("sl_cooldown_enabled", True):
        cd_mins = int(ALERT_STATE.get("sl_cooldown_minutes", 30))
        if cd_mins > 0:
            until = (datetime.now().astimezone() + timedelta(minutes=cd_mins)).strftime("%H:%M")
            full_msg += f"\n\n⏸️ **SL cooldown:** no new signals for `{cd_mins}m` (until `{until}` local)."

    if extra_note:
        full_msg += f"\n\n🤖 {extra_note}"

    try:
        chart_buf = None
        if df_chart is not None and not df_chart.empty:
            chart_buf = await asyncio.to_thread(
                generate_outcome_chart,
                df=df_chart, entry_p=sig["entry_price"], sl_p=sig["sl_price"],
                tp1_p=sig["tp1_price"], tp2_p=sig["tp2_price"],
                outcome_status=new_status, symbol=symbol, direction=sig["direction"],
                entry_time=sig["timestamp"], exit_time=closed_at, dual=bool(sig["dual_entry"]),
                trades=trades_hist, exclude_id=sig["id"], near_zone=_zone_from_sig(sig),
            )
        await send_alert(context.bot, chat_id, full_msg, chart_buf)
    except Exception as e:
        logging.exception(f"Error sending outcome alert for signal {sig['id']}: {e}")


# ---------------------------------------------------------------- the job

async def _enforce_one_trade(context):
    try:
        for line in await asyncio.to_thread(enforce_single_trade):
            if USER_ID:
                await send_text(context.bot, int(USER_ID), line)
    except Exception:
        logging.exception("enforce_single_trade failed")


@job("outcome_tracker", interval=30, first=10)
async def signal_outcome_tracker_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Tracks each active signal's leg(s) to a terminal outcome. The lifecycle rules live in
    core/outcomes.py (see its docstring); this job only observes, acts and announces.
    """
    symbol = await asyncio.to_thread(get_gold_symbol) or "XAUUSD"
    df = await asyncio.to_thread(fetch_candles, symbol, "M1", 5)
    if df is None or df.empty:
        return

    await _enforce_one_trade(context)

    price = round(df["close"].iloc[-1], 2)
    chat_id = context.job.chat_id if (context.job and context.job.chat_id) else int(USER_ID)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    updates, status_writes, be_done = [], [], []     # updates: (sig, new_status, message, extra_note)

    for sig in await asyncio.to_thread(get_tracked_signals):
        # A runner whose break-even trail was never confirmed gets retried every tick.
        if outcomes.be_retry_needed(sig):
            ok, _ = await _trail_to_be(sig)
            if ok:
                be_done.append(sig["id"])

        tr = outcomes.resolve(sig, price, await _broker_exit(sig))
        if tr is None:
            continue

        extra_note = None
        if tr.trail_leg2_to_be:
            ok, extra_note = await _trail_to_be(sig)
            if ok:
                be_done.append(sig["id"])
        if tr.close_leg2_reason:
            extra_note = await _close_runner(sig, tr.close_leg2_reason)

        status_writes.append((tr.new_status, now_str, sig["id"]))
        updates.append((sig, tr.new_status, tr.message, extra_note))

    await asyncio.to_thread(update_signal_statuses, status_writes)
    for sid in be_done:
        mark_leg2_be_moved(sid)

    if not updates:
        return
    await evaluate_circuit_breaker(context)

    # One chart-candle fetch shared by every update in this tick (entry timeframe, deep window).
    entry_tf = ALERT_STATE.get("entry_tf") or "M5"
    df_chart = await asyncio.to_thread(fetch_candles, symbol, entry_tf, 300)
    trades_hist = await asyncio.to_thread(get_recent_trades_for_chart, 40, symbol)
    for sig, new_status, message, extra_note in updates:
        await _announce(context, chat_id, symbol, sig, new_status, message, extra_note,
                        now_str, df_chart, trades_hist)