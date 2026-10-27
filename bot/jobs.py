# bot/jobs.py

import logging
import asyncio
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta
from telegram.ext import ContextTypes
from telegram.error import NetworkError, TelegramError, TimedOut, BadRequest

from config import ALERT_STATE, USER_ID, BOT_START_TIME, save_settings
from database import (
    get_db_connection,
    get_daily_performance_stats,
    mark_leg2_be_moved,
    get_recent_trades_for_chart,
)
from trade_engine import (
    execute_pending_intents, trail_leg_to_breakeven, force_close_leg, reconcile_exit,
    close_all_managed, list_managed_positions, enforce_single_trade,
)
from mt5_engine import get_gold_symbol, check_mt5_alive, fetch_candles, get_tick
from news_engine import news_guard_check, check_news_blockade, check_news_post_release
from strategy.evaluator import analyze_market, evaluate_signals
from strategy.chart import generate_chart_snapshot, generate_outcome_chart

def format_uptime(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours or days: parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)

def _plain(text):
    return text.replace("*", "").replace("`", "")

async def _send_text(bot, chat_id, text, **kw):
    kw.pop("parse_mode", None)  # this helper owns parse_mode
    try:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", **kw)
    except BadRequest as e:
        logging.warning(f"Markdown rejected ({e}); resending as plain text")
        return await bot.send_message(chat_id=chat_id, text=_plain(text), **kw)

async def _send_alert(bot, chat_id, text, photo=None):
    try:
        if photo and len(text) <= 1024:
            try:
                await bot.send_photo(chat_id=chat_id, photo=photo, caption=text, parse_mode="Markdown")
            except BadRequest:
                photo.seek(0)
                await bot.send_photo(chat_id=chat_id, photo=photo, caption=_plain(text))
            return
        if photo:
            await bot.send_photo(chat_id=chat_id, photo=photo)
        await _send_text(bot, chat_id, text)
    except (NetworkError, TelegramError) as e:
        logging.warning(f"⚠️ Alert dispatch failed: {e}")

async def build_status_snapshot() -> str:
    uptime = format_uptime(datetime.now(timezone.utc) - BOT_START_TIME)
    mt5_ok = await asyncio.to_thread(check_mt5_alive)
    ALERT_STATE["mt5_connected"] = mt5_ok
    last_hb = ALERT_STATE["last_heartbeat_at"]
    last_hb_str = last_hb.astimezone().strftime("%Y-%m-%d %H:%M %Z") if last_hb else "Not sent yet"

    trade_mode = "🔴 OFF"
    if ALERT_STATE.get("auto_trade_enabled"):
        trade_mode = "🧪 DRY-RUN" if ALERT_STATE.get("trade_dry_run", True) else "🟢 LIVE"
    try:
        open_managed = len(await asyncio.to_thread(list_managed_positions))
    except Exception:
        open_managed = 0

    return (
        f"• **Uptime:** `{uptime}`\n"
        f"• **MT5 Connection:** {'🟢 Connected' if mt5_ok else '🔴 Disconnected'}\n"
        f"• **Scanner:** {'🟢 Running' if ALERT_STATE['scanner_enabled'] else '🔴 Stopped'}\n"
        f"• **Auto-Trade:** {trade_mode} | **Open Positions:** `{open_managed}`\n"
        f"• **Active Strategy:** `{ALERT_STATE.get('active_strategy', 'smc_confluence').upper()}`\n"
        f"• **Sizing:** `{ALERT_STATE.get('fixed_lot_size', 0.01)}` lots/leg (fixed) | "
        f"**Dual-Entry ≥:** `{ALERT_STATE.get('dual_entry_score_threshold', 50)}`\n"
        f"• **Timeframe Mode:** `{ALERT_STATE['timeframe_mode']}`\n"
        f"• **Structure Filter:** {'ON' if ALERT_STATE['require_structure_break'] else 'OFF'}\n"
        f"• **Volume/ATR Filter:** {'ON' if ALERT_STATE['require_volume_atr_filter'] else 'OFF'}\n"
        f"• **News Lockout:** {'🔒 Active' if ALERT_STATE['news_lockout'] else 'Clear'}\n"
        f"• **Last Signal:** `{ALERT_STATE['last_rsi_signal'] or 'None'}`\n"
        f"• **Heartbeat Pings:** {'🟢 ON' if ALERT_STATE['heartbeat_enabled'] else '🔴 OFF'} every `{ALERT_STATE['heartbeat_interval_hours']}h`\n"
        f"• **Last Heartbeat Sent:** `{last_hb_str}`"
    )

async def evaluate_circuit_breaker(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Evaluates daily performance. If thresholds are breached, shuts down scanner
    and broadcasts an emergency Telegram alert.
    """
    if not ALERT_STATE.get("circuit_breaker_enabled", True):
        return False

    stats = await asyncio.to_thread(get_daily_performance_stats)
    max_losses = ALERT_STATE.get("max_daily_losses", 3)
    max_drawdown = ALERT_STATE.get("max_daily_drawdown_r", 3.0)

    triggered = False
    reason = ""

    if stats["consecutive_losses"] >= max_losses:
        triggered = True
        reason = f"Hit **{stats['consecutive_losses']} consecutive losses** today (Limit: `{max_losses}`)."
    elif stats["net_r"] <= -abs(max_drawdown):
        triggered = True
        reason = f"Daily drawdown reached **{stats['net_r']:.1f}R** (Limit: `-{abs(max_drawdown):.1f}R`)."

    if triggered and ALERT_STATE.get("scanner_enabled"):
        ALERT_STATE["scanner_enabled"] = False
        save_settings()

        current_jobs = context.job_queue.get_jobs_by_name("xauusd_scanner")
        for job in current_jobs:
            job.schedule_removal()

        flatten_note = ""
        if ALERT_STATE.get("flatten_on_circuit_breaker", True):
            results = await asyncio.to_thread(close_all_managed)
            flatten_note = "\n\n🧯 **Flattened managed positions:**\n" + "\n".join(f"• {r}" for r in results)

        msg = (
            f"🚨 **CIRCUIT BREAKER TRIGGERED — SCANNER DEACTIVATED** 🚨\n\n"
            f"• **Reason:** {reason}\n"
            f"• **Today's Net R:** `{stats['net_r']:.1f}R`\n"
            f"• **Today's Record:** `{stats['today_wins']}W - {stats['today_losses']}L`\n\n"
            "🛡️ *Scanner has been paused to protect capital. Review market conditions before re-enabling.*"
            f"{flatten_note}"
        )
        chat_id = context.job.chat_id if (context.job and context.job.chat_id) else int(USER_ID)
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        return True

    return False

async def market_scanner_job(context: ContextTypes.DEFAULT_TYPE):
    if not ALERT_STATE["scanner_enabled"]:
        return
    chat_id = context.job.chat_id or USER_ID
    if not chat_id:
        return
    symbol = await asyncio.to_thread(get_gold_symbol) or "XAUUSD"

    # 1. Circuit Breaker Gate
    if await evaluate_circuit_breaker(context):
        return

    # 2. Pre-Event Warning Check
    await news_guard_check(context, chat_id)

    # 3. Live Outcome Result Broadcast Check
    await check_news_post_release(context, chat_id)

    # 4. News Blackout Guard Gate
    is_blocked, reason = await check_news_blockade()
    ALERT_STATE["news_lockout"] = is_blocked
    if is_blocked:
        logging.info(f"📰 Market scanner paused due to news blockade: {reason}")
        return

    # 5. Spread Guard Gate
    tick = await asyncio.to_thread(get_tick, symbol)
    if tick:
        spread_pips = round((tick.ask - tick.bid) * 10, 1)
        if spread_pips > ALERT_STATE["max_allowed_spread_pips"]:
            logging.info(f"⚠️ Scanner skipped: Spread too high ({spread_pips} pips)")
            return

    # 6. Market Technical Evaluation
    try: 
        analysis = await asyncio.to_thread(analyze_market, symbol)
        if analysis:
            signals_found, watch_found = await asyncio.to_thread(evaluate_signals, analysis)
            
            for item in signals_found:
                if isinstance(item, tuple):
                    msg_text, chart_buffer = item
                    await _send_alert(context.bot, chat_id, msg_text, chart_buffer)
                else:
                    await _send_alert(context.bot, chat_id, item)

            for watch_msg in watch_found:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, 
                        text=watch_msg, 
                        parse_mode="Markdown"
                    )
                except (TimedOut, NetworkError, TelegramError) as e:
                    logging.warning(f"⚠️ Telegram dispatch timed out: {e}")
    except Exception:
        logging.exception("Scanner evaluation failed")

    # 7. Live Execution Drain (blocking MT5 order_send, kept off the event loop)
    try:
        exec_reports = await asyncio.to_thread(execute_pending_intents)
    except Exception as e:
        logging.exception("Trade execution drain crashed")
        exec_reports = [f"❌ **Execution engine error:** `{e}`"]

    for report in exec_reports:
        try:
            await _send_text(context.bot, chat_id=chat_id, text=report, parse_mode="Markdown")
        except (TimedOut, NetworkError, TelegramError) as e:
            logging.warning(f"⚠️ Execution report dispatch failed: {e}")

def _zone_from_sig(sig):
    """Rebuilds the S/R zone that existed when the signal fired (None for pre-migration rows)."""
    if sig["zone_low"] is None or sig["zone_high"] is None:
        return None
    low, high = float(sig["zone_low"]), float(sig["zone_high"])
    return {"type": sig["zone_type"] or "mixed", "low": low, "high": high, "price": (low + high) / 2}

async def signal_outcome_tracker_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Tracks each active signal's leg(s) to a terminal outcome.

    Single-entry signals (dual_entry=0) have one leg: it resolves straight from
    PENDING to CLOSED_TP1 (full win) or HIT_SL (loss) — no runner phase.

    Dual-entry signals (dual_entry=1) have two legs. Leg 1 resolves PENDING ->
    HIT_TP1 (non-terminal: leg 1 won, leg 2's SL gets trailed to break-even and
    is now running for TP2) or -> HIT_SL (leg 1 lost, so leg 2 — which shares
    the same original SL — is force-closed rather than left dangling). From
    HIT_TP1, leg 2 then resolves to the terminal HIT_TP2 or CLOSED_BE.
    """
    symbol = await asyncio.to_thread(get_gold_symbol) or "XAUUSD"
    df = await asyncio.to_thread(fetch_candles, symbol, mt5.TIMEFRAME_M1, 5)
    if df is None or df.empty:
        return

    try:
        for line in await asyncio.to_thread(enforce_single_trade):
            if USER_ID:
                await _send_text(context.bot, chat_id=int(USER_ID), text=line, parse_mode="Markdown")
    except Exception:
        logging.exception("enforce_single_trade failed")

    current_price = round(df['close'].iloc[-1], 2)
    chat_id = context.job.chat_id if (context.job and context.job.chat_id) else int(USER_ID)

    updates_to_send = []   # (sig, new_status, msg, extra_note, now_str)
    status_writes = []
    be_done = []

    with get_db_connection() as conn:
        active_signals = conn.execute(
            "SELECT id, timestamp, status, direction, entry_price, sl_price, tp1_price, tp2_price, dual_entry, "
            "ticket, exec_mode, ticket2, exec_mode2, be_moved2, zone_low, zone_high, zone_type "
            "FROM signals WHERE status IN ('PENDING', 'HIT_TP1')"
        ).fetchall()

    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    for sig in active_signals:
        sig_id = sig["id"]
        status = sig["status"]
        direction = sig["direction"]
        entry, sl, tp1, tp2 = sig["entry_price"], sig["sl_price"], sig["tp1_price"], sig["tp2_price"]
        dual = bool(sig["dual_entry"])
        new_status = None
        msg = None
        extra_note = None

        if status == "PENDING":
            leg1_outcome = None
            live1 = sig["exec_mode"] == "LIVE" and sig["ticket"]
            if live1:
                try:
                    leg1_outcome = await asyncio.to_thread(
                        reconcile_exit, sig["ticket"], {"HIT_SL": sl, "WON_TP1": tp1}
                    )
                except Exception as e:
                    logging.warning(f"Reconcile failed for leg1 #{sig['ticket']}: {e}")

            if leg1_outcome is None and not live1:
                if direction == "BUY":
                    if current_price >= tp1:
                        leg1_outcome = "WON_TP1"
                    elif current_price <= sl:
                        leg1_outcome = "HIT_SL"
                else:
                    if current_price <= tp1:
                        leg1_outcome = "WON_TP1"
                    elif current_price >= sl:
                        leg1_outcome = "HIT_SL"

            if leg1_outcome == "WON_TP1":
                if dual:
                    new_status = "HIT_TP1"
                    msg = (
                        f"🎯 **XAUUSD {direction} — LEG 1 (TP1) HIT!** (${tp1:.2f})\n"
                        f"🛡️ *Leg 2's Stop Loss is being trailed to Break-Even (${entry:.2f}) — running for TP2.*"
                    )
                    if sig["exec_mode2"] == "LIVE" and sig["ticket2"] and not sig["be_moved2"]:
                        try:
                            ok, extra_note = await asyncio.to_thread(trail_leg_to_breakeven, sig["ticket2"], entry)
                            if ok:
                                be_done.append(sig_id)
                        except Exception as e:
                            logging.exception(f"Leg2 BE-trail failed for ticket {sig['ticket2']}: {e}")
                            extra_note = f"SL → BE: ⚠️ error ({e})"
                else:
                    new_status = "CLOSED_TP1"
                    msg = f"🎯 **XAUUSD {direction} — TP1 HIT! FULL CLOSE.** (${tp1:.2f})"

            elif leg1_outcome == "HIT_SL":
                new_status = "HIT_SL"
                msg = f"🛡️ **XAUUSD {direction} — STOP LOSS HIT** (${sl:.2f})"
                if dual and sig["exec_mode2"] == "LIVE" and sig["ticket2"]:
                    try:
                        extra_note = await asyncio.to_thread(force_close_leg, sig["ticket2"], "leg 1 stopped out")
                    except Exception as e:
                        logging.exception(f"Leg2 force-close failed for ticket {sig['ticket2']}: {e}")
                        extra_note = f"Leg 2 force-close: ⚠️ error ({e})"

        elif status == "HIT_TP1":
            # Dual-entry only: leg 1 already won, leg 2 is running with SL at BE.
            leg2_outcome = None
            live2 = sig["exec_mode2"] == "LIVE" and sig["ticket2"]
            if live2:
                if not sig["be_moved2"]:
                    try:
                        ok, _ = await asyncio.to_thread(trail_leg_to_breakeven, sig["ticket2"], entry)
                        if ok:
                            be_done.append(sig_id)
                    except Exception:
                        logging.exception("BE retry failed")
                try:
                    leg2_outcome = await asyncio.to_thread(
                        reconcile_exit, sig["ticket2"], {"CLOSED_BE": entry, "HIT_TP2": tp2}
                    )
                except Exception as e:
                    logging.warning(f"Reconcile failed for leg2 #{sig['ticket2']}: {e}")

            if leg2_outcome is None and not live2:
                if direction == "BUY":
                    if current_price >= tp2:
                        leg2_outcome = "HIT_TP2"
                    elif current_price <= entry:
                        leg2_outcome = "CLOSED_BE"
                else:
                    if current_price <= tp2:
                        leg2_outcome = "HIT_TP2"
                    elif current_price >= entry:
                        leg2_outcome = "CLOSED_BE"

            if leg2_outcome == "HIT_TP2":
                new_status = "HIT_TP2"
                msg = f"🚀 **XAUUSD {direction} — LEG 2 TP2 HIT!** (${tp2:.2f}) — Full Target Reached!"
            elif leg2_outcome == "CLOSED_BE":
                new_status = "CLOSED_BE"
                msg = f"🔒 **XAUUSD {direction} — LEG 2 CLOSED AT BREAK-EVEN** (${entry:.2f})"

        if new_status:
            status_writes.append((new_status, now_str, sig_id))
            updates_to_send.append((sig, new_status, msg, extra_note, now_str))

    if status_writes:
        with get_db_connection() as conn:
            conn.executemany(
                "UPDATE signals SET status = ?, updated_at = ? WHERE id = ?", status_writes
            )
            conn.commit()

    for sid in be_done:
        mark_leg2_be_moved(sid)

    if not updates_to_send:
        return
    await evaluate_circuit_breaker(context)

    # Chart on the ENTRY timeframe with a deep window (not a hardcoded 60 x M5),
    # fetched once and shared by every update in this tick.
    entry_tf = ALERT_STATE.get("entry_tf", mt5.TIMEFRAME_M5)
    df_chart = await asyncio.to_thread(fetch_candles, symbol, entry_tf, 300)
    trades_hist = await asyncio.to_thread(get_recent_trades_for_chart, 40, symbol)
    for sig, new_status, msg, extra_note, closed_at in updates_to_send:
        entry, sl, tp1, tp2 = sig["entry_price"], sig["sl_price"], sig["tp1_price"], sig["tp2_price"]
        if not (chat_id and msg):
            continue
        full_msg = msg + (f"\n\n🤖 {extra_note}" if extra_note else "")
        try:
            chart_buf = None
            if df_chart is not None and not df_chart.empty:
                chart_buf = await asyncio.to_thread(
                    generate_outcome_chart,
                    df=df_chart, entry_p=entry, sl_p=sl, tp1_p=tp1, tp2_p=tp2,
                    outcome_status=new_status, symbol=symbol,
                    direction=sig["direction"],
                    entry_time=sig["timestamp"],      # true UTC, ISO
                    exit_time=closed_at,              # true UTC, 'YYYY-MM-DD HH:MM:SS'
                    dual=bool(sig["dual_entry"]),
                    trades=trades_hist, exclude_id=sig["id"],
                    near_zone=_zone_from_sig(sig),
                )
            await _send_alert(context.bot, chat_id, full_msg, chart_buf)
        except Exception as e:
            logging.exception(f"Error sending outcome alert for signal {sig['id']}: {e}")

async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    if not ALERT_STATE.get("heartbeat_enabled"):
        return
    chat_id = context.job.chat_id
    if not chat_id: return
    snapshot = await build_status_snapshot()
    ALERT_STATE["last_heartbeat_at"] = datetime.now(timezone.utc)
    await context.bot.send_message(chat_id=chat_id, text=f"💓 **HEARTBEAT — BOT IS ALIVE**\n\n{snapshot}", parse_mode="Markdown")

async def mt5_watchdog_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    if not chat_id: return
    mt5_ok = await asyncio.to_thread(check_mt5_alive)
    was_ok = ALERT_STATE["mt5_connected"]

    if mt5_ok:
        ALERT_STATE["consecutive_mt5_failures"] = 0
        if not was_ok:
            ALERT_STATE["mt5_connected"] = True
            await context.bot.send_message(chat_id=chat_id, text="✅ **MT5 CONNECTION RESTORED**\n\nThe bot has reconnected to MT5. Scanning and signals have resumed.", parse_mode="Markdown")
        return

    ALERT_STATE["consecutive_mt5_failures"] += 1
    if was_ok and ALERT_STATE["consecutive_mt5_failures"] >= 2:
        ALERT_STATE["mt5_connected"] = False
        await context.bot.send_message(chat_id=chat_id, text="🚨 **MT5 CONNECTION LOST!**\n\nThe bot can't reach the MT5 terminal. Signal generation is effectively paused.", parse_mode="Markdown")

def ensure_watchdog_running(job_queue, chat_id):
    if not job_queue.get_jobs_by_name("mt5_watchdog"):
        job_queue.run_repeating(mt5_watchdog_job, interval=90, first=15, chat_id=chat_id, name="mt5_watchdog", job_kwargs={"misfire_grace_time": 30})

def restart_heartbeat_job(job_queue, chat_id):
    for job in job_queue.get_jobs_by_name("heartbeat_ping"):
        job.schedule_removal()
    if ALERT_STATE["heartbeat_enabled"]:
        interval_seconds = ALERT_STATE["heartbeat_interval_hours"] * 3600
        job_queue.run_repeating(heartbeat_job, interval=interval_seconds, first=10, chat_id=chat_id, name="heartbeat_ping", job_kwargs={"misfire_grace_time": 30})