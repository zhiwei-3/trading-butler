import sqlite3
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta
from telegram.ext import ContextTypes
from config import ALERT_STATE, YOUR_CHAT_ID, BOT_START_TIME, DB_FILE
from mt5_engine import get_gold_symbol, check_mt5_alive
from news_engine import news_guard_check
from strategy.evaluator import analyze_market, evaluate_signals

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
    """Builds a diagnostic summary string for heartbeat pings and /status checks."""
    uptime = format_uptime(datetime.now(timezone.utc) - BOT_START_TIME)
    mt5_ok = check_mt5_alive()
    ALERT_STATE["mt5_connected"] = mt5_ok
    last_hb = ALERT_STATE["last_heartbeat_at"]
    last_hb_str = last_hb.strftime("%Y-%m-%d %H:%M UTC") if last_hb else "Not sent yet"

    return (
        f"• **Uptime:** `{uptime}`\n"
        f"• **MT5 Connection:** {'🟢 Connected' if mt5_ok else '🔴 Disconnected'}\n"
        f"• **Scanner:** {'🟢 Running' if ALERT_STATE['scanner_enabled'] else '🔴 Stopped'}\n"
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
        for msg in signals_found + watch_found:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

async def signal_outcome_tracker_job(context: ContextTypes.DEFAULT_TYPE):
    """Monitors active pending signals against live price ticks to log win/loss outcomes."""
    symbol = get_gold_symbol()
    if not symbol: return
    tick = mt5.symbol_info_tick(symbol)
    if not tick: return

    bid, ask = tick.bid, tick.ask
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, direction, entry_price, sl_price, tp1_price, tp2_price FROM signals WHERE status = 'PENDING'")
    pending = cursor.fetchall()

    for sig_id, direction, entry_p, sl_p, tp1_p, tp2_p in pending:
        now_str = datetime.now(timezone.utc).isoformat()
        if direction == 'BUY':
            if bid <= sl_p: cursor.execute("UPDATE signals SET status = 'HIT_SL', closed_at = ? WHERE id = ?", (now_str, sig_id))
            elif ask >= tp2_p: cursor.execute("UPDATE signals SET status = 'HIT_TP2', closed_at = ? WHERE id = ?", (now_str, sig_id))
            elif ask >= tp1_p: cursor.execute("UPDATE signals SET status = 'HIT_TP1', closed_at = ? WHERE id = ?", (now_str, sig_id))
        elif direction == 'SELL':
            if ask >= sl_p: cursor.execute("UPDATE signals SET status = 'HIT_SL', closed_at = ? WHERE id = ?", (now_str, sig_id))
            elif bid <= tp2_p: cursor.execute("UPDATE signals SET status = 'HIT_TP2', closed_at = ? WHERE id = ?", (now_str, sig_id))
            elif bid <= tp1_p: cursor.execute("UPDATE signals SET status = 'HIT_TP1', closed_at = ? WHERE id = ?", (now_str, sig_id))

    conn.commit()
    conn.close()

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
    if not mt5_ok and ALERT_STATE["mt5_connected"]:
        ALERT_STATE["mt5_connected"] = False
        await context.bot.send_message(chat_id=chat_id, text="🚨 **MT5 DISCONNECTED!**", parse_mode="Markdown")
    elif mt5_ok and not ALERT_STATE["mt5_connected"]:
        ALERT_STATE["mt5_connected"] = True
        await context.bot.send_message(chat_id=chat_id, text="✅ **MT5 RECONNECTED!**", parse_mode="Markdown")