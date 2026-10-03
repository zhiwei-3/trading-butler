from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import USER_ID, ALERT_STATE, save_settings
from database import get_signal_stats
from bot.jobs import (
    build_status_snapshot,
    market_scanner_job,
    ensure_watchdog_running,
    restart_heartbeat_job,
)
from ._common import admin_only


@admin_only
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_watchdog_running(context.job_queue, chat_id)

    await update.message.reply_text(
        "🤵‍♂️ **Trading Butler Online (Full Stack Modular)**\n\n"
        "**🎛️ Core & Dashboards**\n"
        "• `/menu` - Interactive Control Dashboard\n"
        "• `/scanner_on` / `/scanner_off` - Toggle Market Scanner\n"
        "• `/status` - Full Bot Health Check\n"
        "• `/stats` - Forward-Testing Performance & Win Rate\n\n"
        "**🤖 Trade Execution**\n"
        "• `/trade` - Arm/Disarm Live Execution (Dry/Live)\n"
        "• `/positions` - View Open Managed Positions\n"
        "• `/close <ticket|all>` - Close Position or Flatten All\n"
        "• `/breakeven <ticket>` - Trail SL to Entry Price\n"
        "• `/calc <bal> <risk%> <sl>` - Manual Position Size Reference\n\n"
        "**📊 Market Analysis**\n"
        "• `/gold` - Gold Technical Snapshot\n"
        "• `/spread` - Live Bid/Ask & Spread Guard Status\n"
        "• `/news [day] [impact]` - USD Calendar (e.g. `/news today`, `/news tue high`)\n"
        "• `/session` - Market Session Clock\n"
        "• `/diagnose` - Live Signal Diagnostic Check\n\n"
        "**⚙️ Strategy & Parameters**\n"
        "• `/strategy` - Switch Active Trading Strategy\n"
        "• `/timeframe <mode>` - Switch Strategy Timeframe\n"
        "• `/set <key> <val>` - View/Adjust Dynamic Parameters\n"
        "• `/filters` - View/Adjust Structure & Volume Filters\n"
        "• `/confluence <0-100>` - View/Adjust Min Confidence Score\n"
        "• `/watchlist` - View/Adjust 'Setup Forming' Pings\n\n"
        "**🧪 Testing & Utility**\n"
        "• `/backtest <days> [mode]` - Historical Strategy Replay\n"
        "• `/optimize [days] [mode] [flags]` - Parameter Sweep\n"
        "• `/heartbeat` - View/Adjust Periodic Pings\n\n"
        "💓 24/7 monitoring is active in this chat.",
        parse_mode="Markdown"
    )


@admin_only
async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays an interactive control menu with dynamic inline buttons."""
    scanner_status = "🔴 OFF" if not ALERT_STATE.get("scanner_enabled") else "🟢 ON"
    strat = ALERT_STATE.get("active_strategy", "smc_confluence").replace("_", " ").upper()
    tf_mode = ALERT_STATE.get("timeframe_mode", "scalp").upper()

    text = (
        "🎛️ **TRADING BUTLER CONTROL DASHBOARD**\n\n"
        f"• **Scanner:** `{scanner_status}`\n"
        f"• **Active Strategy:** `{strat}`\n"
        f"• **Timeframe Preset:** `{tf_mode}`\n"
        f"• **Min Confluence Score:** `{ALERT_STATE.get('min_confluence_score')}/100`\n"
        f"• **Min RRR:** `1:{ALERT_STATE.get('min_rrr')}`\n"
        f"• **Sizing:** `{ALERT_STATE.get('fixed_lot_size', 0.01)}` lots/leg | "
        f"**Dual-Entry ≥** `{ALERT_STATE.get('dual_entry_score_threshold', 50)}`\n\n"
        "Tap a button below for instant quick actions:"
    )

    keyboard = [
        [
            InlineKeyboardButton("🟢 Scanner ON", callback_data="btn_scanner_on"),
            InlineKeyboardButton("🔴 Scanner OFF", callback_data="btn_scanner_off"),
        ],
        [
            InlineKeyboardButton("📊 Gold Chart", callback_data="btn_gold"),
            InlineKeyboardButton("🔍 Spread Check", callback_data="btn_spread"),
            InlineKeyboardButton("📈 Stats", callback_data="btn_stats"),
        ],
        [
            InlineKeyboardButton("⚙️ Strategy Selector", callback_data="btn_strat_menu"),
            InlineKeyboardButton("🕒 Timeframe Selector", callback_data="btn_tf_menu"),
        ],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


@admin_only
async def enable_scanner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ALERT_STATE["scanner_enabled"] = True
    save_settings()
    current_jobs = context.job_queue.get_jobs_by_name("xauusd_scanner")
    for job in current_jobs:
        job.schedule_removal()

    context.job_queue.run_repeating(
        market_scanner_job, interval=60, first=5, chat_id=chat_id, name="xauusd_scanner",
        job_kwargs={"misfire_grace_time": 30}
    )
    ensure_watchdog_running(context.job_queue, chat_id)
    if not USER_ID:
        restart_heartbeat_job(context.job_queue, chat_id)
    await update.effective_message.reply_text("🟢 **Market Scanner Activated!** Checking XAUUSD every 60 seconds.", parse_mode="Markdown")


@admin_only
async def disable_scanner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ALERT_STATE["scanner_enabled"] = False
    save_settings()
    current_jobs = context.job_queue.get_jobs_by_name("xauusd_scanner")
    for job in current_jobs:
        job.schedule_removal()
    await update.effective_message.reply_text("🔴 **Market Scanner Deactivated.**", parse_mode="Markdown")


@admin_only
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    snapshot = await build_status_snapshot()
    await update.message.reply_text(f"🩺 **BOT STATUS CHECK**\n\n{snapshot}", parse_mode="Markdown")


@admin_only
async def heartbeat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    chat_id = update.effective_chat.id
    ensure_watchdog_running(context.job_queue, chat_id)
    if not args:
        status = "🟢 ON" if ALERT_STATE["heartbeat_enabled"] else "🔴 OFF"
        await update.message.reply_text(f"💓 **HEARTBEAT:** {status} every `{ALERT_STATE['heartbeat_interval_hours']}h`\n\n**Usage:** `/heartbeat on|off|test`", parse_mode="Markdown")
        return
    sub = args[0].lower()
    if sub == "test":
        snapshot = await build_status_snapshot()
        await update.message.reply_text(f"💓 **TEST HEARTBEAT**\n\n{snapshot}", parse_mode="Markdown")
    elif sub == "on":
        ALERT_STATE["heartbeat_enabled"] = True
        restart_heartbeat_job(context.job_queue, chat_id)
        save_settings()
        await update.message.reply_text("✅ Heartbeat **ON**", parse_mode="Markdown")
    elif sub == "off":
        ALERT_STATE["heartbeat_enabled"] = False
        restart_heartbeat_job(context.job_queue, chat_id)
        save_settings()
        await update.message.reply_text("🔴 Heartbeat **OFF**", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ **Unknown option.** Usage: `/heartbeat on|off|test`", parse_mode="Markdown")


@admin_only
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    counts = get_signal_stats()
    pending = counts.get('PENDING', 0)
    running = counts.get('HIT_TP1', 0)          # dual-entry: leg1 won, leg2 still running
    closed_tp1 = counts.get('CLOSED_TP1', 0)    # single-entry: full win at TP1
    tp2 = counts.get('HIT_TP2', 0)              # dual-entry: leg2 won
    be = counts.get('CLOSED_BE', 0)             # dual-entry: leg2 stopped at BE
    sl = counts.get('HIT_SL', 0)

    # HIT_TP1 is a still-open runner (dual-entry leg2 pending) — NOT a closed win.
    wins = closed_tp1 + tp2 + be
    total_closed = wins + sl
    win_rate = round((wins / total_closed * 100), 1) if total_closed > 0 else 0.0

    reply = (
        f"📊 **FORWARD-TESTING PERFORMANCE STATS**\n\n"
        f"• **Total Signals:** `{sum(counts.values())}` | **Pending Entry:** `{pending}`\n"
        f"• **Dual-Entry Runners (leg2 in flight):** `{running}` 🎯 *(open — not counted below)*\n"
        f"• **Single-Entry TP1 Wins:** `{closed_tp1}` 🎯\n"
        f"• **TP2 Hits (leg2):** `{tp2}` 🚀\n"
        f"• **Break-Even Closes (leg2):** `{be}` 🔒\n"
        f"• **Stop Loss Hits:** `{sl}` 🛡️\n\n"
        f"📈 **Win Rate:** `{win_rate}%` (`{wins}/{total_closed}` closed trades)"
    )
    await update.effective_message.reply_text(reply, parse_mode="Markdown")