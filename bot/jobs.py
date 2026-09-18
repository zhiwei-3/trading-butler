import logging
import asyncio
import sqlite3
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta
from telegram.ext import ContextTypes
from telegram.error import NetworkError, TelegramError, TimedOut

from config import ALERT_STATE, USER_ID, BOT_START_TIME, DB_FILE, save_settings
from database import get_db_connection, get_daily_performance_stats, mark_trade_flags
from trade_engine import (
    execute_pending_intents, handle_tp1, reconcile_ticket,
    close_all_managed, list_managed_positions,
)
from mt5_engine import MT5_LOCK, get_gold_symbol, check_mt5_alive, fetch_candles
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

def build_status_snapshot() -> str:
    uptime = format_uptime(datetime.now(timezone.utc) - BOT_START_TIME)
    mt5_ok = check_mt5_alive()
    ALERT_STATE["mt5_connected"] = mt5_ok
    last_hb = ALERT_STATE["last_heartbeat_at"]
    last_hb_str = last_hb.strftime("%Y-%m-%d %H:%M UTC") if last_hb else "Not sent yet"

    trade_mode = "🔴 OFF"
    if ALERT_STATE.get("auto_trade_enabled"):
        trade_mode = "🧪 DRY-RUN" if ALERT_STATE.get("trade_dry_run", True) else "🟢 LIVE"
    try:
        open_managed = len(list_managed_positions())
    except Exception:
        open_managed = 0

    return (
        f"• **Uptime:** `{uptime}`\n"
        f"• **MT5 Connection:** {'🟢 Connected' if mt5_ok else '🔴 Disconnected'}\n"
        f"• **Scanner:** {'🟢 Running' if ALERT_STATE['scanner_enabled'] else '🔴 Stopped'}\n"
        f"• **Auto-Trade:** {trade_mode} | **Open Positions:** `{open_managed}`\n"
        f"• **Active Strategy:** `{ALERT_STATE.get('active_strategy', 'smc_confluence').upper()}`\n"
        f"• **Risk Sizing:** `{ALERT_STATE.get('risk_percent', 1.0)}%` per trade\n"
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

    stats = get_daily_performance_stats()
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

        flatten_note = ""
        if ALERT_STATE.get("flatten_on_circuit_breaker", True) and list_managed_positions():
            results = await asyncio.to_thread(close_all_managed)
            flatten_note = "\n\n🧯 **Flattened managed positions:**\n" + "\n".join(f"• {r}" for r in results)

        current_jobs = context.job_queue.get_jobs_by_name("xauusd_scanner")
        for job in current_jobs:
            job.schedule_removal()

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
    symbol = get_gold_symbol() or "XAUUSD"

    # 1. Circuit Breaker Gate
    if await evaluate_circuit_breaker(context):
        return

    # 2. Pre-Event Warning Check
    await news_guard_check(context, chat_id)

    # 3. Live Outcome Result Broadcast Check
    await check_news_post_release(context, chat_id)

    # 4. News Blackout Guard Gate
    is_blocked, reason = await check_news_blockade()
    if is_blocked:
        logging.info(f"📰 Market scanner paused due to news blockade: {reason}")
        return

    # 5. Spread Guard Gate
    tick = mt5.symbol_info_tick(symbol)
    if tick:
        spread_pips = round((tick.ask - tick.bid) * 10, 1)
        if spread_pips > ALERT_STATE["max_allowed_spread_pips"]:
            logging.info(f"⚠️ Scanner skipped: Spread too high ({spread_pips} pips)")
            return

    # 6. Market Technical Evaluation
    analysis = analyze_market(symbol)
    if analysis:
        signals_found, watch_found = evaluate_signals(analysis)
        
        for item in signals_found:
            if isinstance(item, tuple):
                msg_text, chart_buffer = item
                if chart_buffer:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=chart_buffer,
                        caption=msg_text,
                        parse_mode="Markdown"
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat_id, 
                        text=msg_text, 
                        parse_mode="Markdown"
                    )
            else:
                await context.bot.send_message(
                    chat_id=chat_id, 
                    text=item, 
                    parse_mode="Markdown"
                )

        for watch_msg in watch_found:
            try:
                await context.bot.send_message(
                    chat_id=chat_id, 
                    text=watch_msg, 
                    parse_mode="Markdown"
                )
            except (TimedOut, NetworkError, TelegramError) as e:
                    logging.warning(f"⚠️ Telegram dispatch timed out: {e}")

    # 7. Live Execution Drain (blocking MT5 order_send, kept off the event loop)
    try:
        exec_reports = await asyncio.to_thread(execute_pending_intents)
    except Exception as e:
        logging.exception("Trade execution drain crashed")
        exec_reports = [f"❌ **Execution engine error:** `{e}`"]

    for report in exec_reports:
        try:
            await context.bot.send_message(chat_id=chat_id, text=report, parse_mode="Markdown")
        except (TimedOut, NetworkError, TelegramError) as e:
            logging.warning(f"⚠️ Execution report dispatch failed: {e}")

async def signal_outcome_tracker_job(context: ContextTypes.DEFAULT_TYPE):
    symbol = get_gold_symbol() or "XAUUSD"
    df = fetch_candles(symbol, mt5.TIMEFRAME_M1, 5)
    if df is None or df.empty:
        return

    current_price = round(df['close'].iloc[-1], 2)
    chat_id = context.job.chat_id if (context.job and context.job.chat_id) else int(USER_ID)

    updates_to_send = []

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, status, direction, entry_price, sl_price, tp1_price, tp2_price, "
            "ticket, lots, exec_mode, tp1_partial_done, be_moved "
            "FROM signals WHERE status IN ('PENDING', 'HIT_TP1')"
        )
        active_signals = cursor.fetchall()
        now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

        for sig in active_signals:
            sig_id = sig["id"]
            status = sig["status"]
            direction = sig["direction"]
            entry, sl = sig["entry_price"], sig["sl_price"]
            tp1, tp2 = sig["tp1_price"], sig["tp2_price"]
            ticket = sig["ticket"]
            new_status = None
            msg = None

            # A live position closed broker-side between polls -> trust deal history
            if ticket and sig["exec_mode"] == "LIVE":
                try:
                    reconciled = await asyncio.to_thread(reconcile_ticket, ticket, status)
                except Exception as e:
                    logging.warning(f"Reconcile failed for #{ticket}: {e}")
                    reconciled = None
                if reconciled:
                    cursor.execute(
                        "UPDATE signals SET status = ?, updated_at = ? WHERE id = ?",
                        (reconciled, now_str, sig_id)
                    )
                    updates_to_send.append((sig, reconciled,
                        f"📕 **XAUUSD {direction} — POSITION CLOSED BY BROKER** (`#{ticket}`)\n"
                        f"• Reconciled outcome: `{reconciled}`"))
                    continue

            if direction == "BUY":
                if status == "PENDING":
                    if current_price >= tp1:
                        new_status = "HIT_TP1"
                        msg = f"🎯 **XAUUSD BUY — TP1 HIT!** (${tp1:.2f})\n🛡️ *Stop Loss moved to Break-Even (${entry:.2f})*"
                    elif current_price <= sl:
                        new_status = "HIT_SL"
                        msg = f"🛡️ **XAUUSD BUY — STOP LOSS HIT** (${sl:.2f})"
                elif status == "HIT_TP1":
                    if current_price >= tp2:
                        new_status = "HIT_TP2"
                        msg = f"🚀 **XAUUSD BUY — TP2 HIT!** (${tp2:.2f}) — Full Target Reached!"
                    elif current_price <= entry:
                        new_status = "CLOSED_BE"
                        msg = f"🔒 **XAUUSD BUY — RUNNER CLOSED AT BREAK-EVEN** (${entry:.2f})"

            elif direction == "SELL":
                if status == "PENDING":
                    if current_price <= tp1:
                        new_status = "HIT_TP1"
                        msg = f"🎯 **XAUUSD SELL — TP1 HIT!** (${tp1:.2f})\n🛡️ *Stop Loss moved to Break-Even (${entry:.2f})*"
                    elif current_price >= sl:
                        new_status = "HIT_SL"
                        msg = f"🛡️ **XAUUSD SELL — STOP LOSS HIT** (${sl:.2f})"
                elif status == "HIT_TP1":
                    if current_price <= tp2:
                        new_status = "HIT_TP2"
                        msg = f"🚀 **XAUUSD SELL — TP2 HIT!** (${tp2:.2f}) — Full Target Reached!"
                    elif current_price >= entry:
                        new_status = "CLOSED_BE"
                        msg = f"🔒 **XAUUSD SELL — RUNNER CLOSED AT BREAK-EVEN** (${entry:.2f})"

            if new_status:
                cursor.execute(
                    "UPDATE signals SET status = ?, updated_at = ? WHERE id = ?",
                    (new_status, now_str, sig_id)
                )
                updates_to_send.append((sig, new_status, msg))

        conn.commit()

    # Broker-side management: bank part of the trade at TP1 and trail SL to BE
    for sig, new_status, _ in updates_to_send:
        if new_status != "HIT_TP1" or not sig["ticket"] or sig["tp1_partial_done"]:
            continue
        try:
            note = await asyncio.to_thread(handle_tp1, sig["ticket"], sig["entry_price"])
            mark_trade_flags(sig["id"], tp1_partial_done=1, be_moved=1)
            if chat_id:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🤖 **TP1 MANAGEMENT — `#{sig['ticket']}`**\n{note}",
                    parse_mode="Markdown"
                )
        except Exception as e:
            logging.exception(f"TP1 management failed for ticket {sig['ticket']}: {e}")

    if updates_to_send:
        await evaluate_circuit_breaker(context)

    for sig, new_status, msg in updates_to_send:
        entry, sl, tp1, tp2 = sig["entry_price"], sig["sl_price"], sig["tp1_price"], sig["tp2_price"]
        if chat_id and msg:
            try:
                df_chart = fetch_candles(symbol, mt5.TIMEFRAME_M5, 60)
                chart_buf = None
                if df_chart is not None and not df_chart.empty:
                    chart_buf = await asyncio.to_thread(
                        generate_outcome_chart,
                        df=df_chart, entry_p=entry, sl_p=sl, tp1_p=tp1, tp2_p=tp2,
                        outcome_status=new_status, symbol=symbol
                    )
                if chart_buf:
                    await context.bot.send_photo(chat_id=chat_id, photo=chart_buf, caption=msg, parse_mode="Markdown")
                else:
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            except (NetworkError, TelegramError) as e:
                logging.warning(f"⚠️ Telegram alert skipped due to network issue: {e}")
            except Exception as e:
                logging.exception(f"Error sending outcome alert for signal {sig['id']}: {e}")

async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    if not chat_id: return
    snapshot = build_status_snapshot()
    ALERT_STATE["last_heartbeat_at"] = datetime.now(timezone.utc)
    await context.bot.send_message(chat_id=chat_id, text=f"💓 **HEARTBEAT — BOT IS ALIVE**\n\n{snapshot}", parse_mode="Markdown")

async def mt5_watchdog_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    if not chat_id: return
    mt5_ok = check_mt5_alive()
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
        job_queue.run_repeating(mt5_watchdog_job, interval=90, first=15, chat_id=chat_id, name="mt5_watchdog")

def restart_heartbeat_job(job_queue, chat_id):
    for job in job_queue.get_jobs_by_name("heartbeat_ping"):
        job.schedule_removal()
    if ALERT_STATE["heartbeat_enabled"]:
        interval_seconds = ALERT_STATE["heartbeat_interval_hours"] * 3600
        job_queue.run_repeating(heartbeat_job, interval=interval_seconds, first=10, chat_id=chat_id, name="heartbeat_ping")