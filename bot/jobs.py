import sqlite3
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta
from telegram.ext import ContextTypes
from config import ALERT_STATE, YOUR_CHAT_ID, BOT_START_TIME, DB_FILE
from database import get_db_connection
from mt5_engine import MT5_LOCK, get_gold_symbol, check_mt5_alive, fetch_candles
from news_engine import news_guard_check
from strategy.evaluator import analyze_market, evaluate_signals
from strategy.chart import generate_chart_snapshot

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

    return (
        f"• **Uptime:** `{uptime}`\n"
        f"• **MT5 Connection:** {'🟢 Connected' if mt5_ok else '🔴 Disconnected'}\n"
        f"• **Scanner:** {'🟢 Running' if ALERT_STATE['scanner_enabled'] else '🔴 Stopped'}\n"
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

async def market_scanner_job(context: ContextTypes.DEFAULT_TYPE):
    if not ALERT_STATE["scanner_enabled"]:
        return
    chat_id = context.job.chat_id or YOUR_CHAT_ID
    if not chat_id:
        return
    symbol = get_gold_symbol()
    if not symbol:
        return
    if await news_guard_check(context, chat_id):
        return

    analysis = analyze_market(symbol)
    if analysis:
        signals_found, watch_found = evaluate_signals(analysis)
        
        # Dispatch executable signal alerts with position sizing & annotated charts
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

        # Dispatch early watch pings as text-only messages
        for watch_msg in watch_found:
            await context.bot.send_message(
                chat_id=chat_id, 
                text=watch_msg, 
                parse_mode="Markdown"
            )

async def signal_outcome_tracker_job(context):
    symbol = get_gold_symbol() or "XAUUSD"
    df = fetch_candles(symbol, mt5.TIMEFRAME_M1, 5)
    if df is None or df.empty:
        return

    current_price = round(df['close'].iloc[-1], 2)

    with get_db_connection() as conn:
        cursor = conn.cursor()
        # Stage 1: Fetch both PENDING trades and TP1 runners
        cursor.execute(
            "SELECT id, status, direction, entry_price, sl_price, tp1_price, tp2_price "
            "FROM signals WHERE status IN ('PENDING', 'HIT_TP1')"
        )
        active_signals = cursor.fetchall()

        for sig in active_signals:
            sig_id, status, direction, entry, sl, tp1, tp2 = sig
            new_status = None
            msg = None

            if direction == "BUY":
                if status == "PENDING":
                    if current_price >= tp1:
                        new_status = "HIT_TP1"
                        msg = f"🎯 **XAUUSD BUY — TP1 HIT!** (${tp1})\n🛡️ *Stop Loss moved to Break-Even (${entry})*"
                    elif current_price <= sl:
                        new_status = "HIT_SL"
                        msg = f"🛡️ **XAUUSD BUY — STOP LOSS HIT** (${sl})"

                elif status == "HIT_TP1":
                    if current_price >= tp2:
                        new_status = "HIT_TP2"
                        msg = f"🚀 **XAUUSD BUY — TP2 HIT!** (${tp2}) — Full Target Reached!"
                    elif current_price <= entry:
                        new_status = "CLOSED_BE"
                        msg = f"🔒 **XAUUSD BUY — RUNNER CLOSED AT BREAK-EVEN** (${entry})"

            elif direction == "SELL":
                if status == "PENDING":
                    if current_price <= tp1:
                        new_status = "HIT_TP1"
                        msg = f"🎯 **XAUUSD SELL — TP1 HIT!** (${tp1})\n🛡️ *Stop Loss moved to Break-Even (${entry})*"
                    elif current_price >= sl:
                        new_status = "HIT_SL"
                        msg = f"🛡️ **XAUUSD SELL — STOP LOSS HIT** (${sl})"

                elif status == "HIT_TP1":
                    if current_price <= tp2:
                        new_status = "HIT_TP2"
                        msg = f"🚀 **XAUUSD SELL — TP2 HIT!** (${tp2}) — Full Target Reached!"
                    elif current_price >= entry:
                        new_status = "CLOSED_BE"
                        msg = f"🔒 **XAUUSD SELL — RUNNER CLOSED AT BREAK-EVEN** (${entry})"

            # Update DB and notify Telegram if state changed
            if new_status:
                cursor.execute("UPDATE signals SET status = ? WHERE id = ?", (new_status, sig_id))
                conn.commit()
                if msg and context.job.chat_id:
                    await context.bot.send_message(chat_id=context.job.chat_id, text=msg, parse_mode="Markdown")

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
    ALERT_STATE["heartbeat_chat_id"] = chat_id
    if ALERT_STATE["heartbeat_enabled"]:
        interval_seconds = ALERT_STATE["heartbeat_interval_hours"] * 3600
        job_queue.run_repeating(heartbeat_job, interval=interval_seconds, first=10, chat_id=chat_id, name="heartbeat_ping")