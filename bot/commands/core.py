# bot/commands/core.py

import logging
import asyncio
from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import USER_ID, ALERT_STATE, save_settings
from database import get_signal_stats
from mt5_engine import check_mt5_alive
from bot.jobs import (
    build_status_snapshot,
    market_scanner_job,
    ensure_watchdog_running,
    restart_heartbeat_job,
)
from ._common import admin_only


def _fetch_account_data(days: int = 1):
    """Synchronous MT5 worker function to fetch account state and deal history."""
    if not check_mt5_alive():
        return None, "🔴 MT5 terminal is not connected or unreachable."

    info = mt5.account_info()
    if info is None:
        return None, "❌ Failed to retrieve MT5 account details."

    now = datetime.now(timezone.utc)
    from_date = now - timedelta(days=days)
    deals = mt5.history_deals_get(from_date, now)

    closed_deals = []
    if deals:
        for d in deals:
            # Entry OUT (1), INOUT (2), or OUT_BY (3) indicates a closed trade leg
            if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT, mt5.DEAL_ENTRY_OUT_BY):
                if d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
                    net_pnl = d.profit + d.swap + d.commission
                    closed_deals.append({
                        "ticket": d.ticket,
                        "order": d.order,
                        "time": datetime.fromtimestamp(d.time, tz=timezone.utc),
                        "symbol": d.symbol,
                        "type": "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
                        "volume": d.volume,
                        "price": d.price,
                        "profit": d.profit,
                        "swap": d.swap,
                        "commission": d.commission,
                        "net_pnl": net_pnl,
                    })

    return {
        "login": info.login,
        "server": info.server,
        "currency": info.currency or "USD",
        "leverage": info.leverage,
        "balance": info.balance,
        "equity": info.equity,
        "profit": info.profit,
        "margin": info.margin,
        "free_margin": info.margin_free,
        "margin_level": info.margin_level if info.margin > 0 else 0.0,
        "closed_deals": closed_deals,
    }, None


@admin_only
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_watchdog_running(context.job_queue, chat_id)

    await update.message.reply_text(
        "🤵‍♂️ **Trading Butler Online (Full Stack Modular)**\n\n"
        "**🎛️ Core & Dashboards**\n"
        "• `/menu` - Interactive Control Dashboard\n"
        "• `/account [days]` - Account Balance, Equity, PnL & History (`/account 7d`)\n"
        "• `/scanner_on` / `/scanner_off` - Toggle Market Scanner\n"
        "• `/status` - Full Bot Health Check\n"
        "• `/stats` - Forward-Testing Performance & Win Rate\n\n"
        "**🤖 Trade Execution**\n"
        "• `/trade` - Arm/Disarm Live Execution (Dry/Live)\n"
        "• `/positions` - View Open Managed Positions\n"
        "• `/close <ticket|all>` - Close Position or Flatten All\n"
        "• `/breakeven <ticket>` - Trail SL to Entry Price\n\n"
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
            InlineKeyboardButton("💳 Account", callback_data="btn_account"),
            InlineKeyboardButton("📊 Gold Chart", callback_data="btn_gold"),
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
async def account_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches real-time MT5 account balance, floating PnL, closed PnL, and recent trade history."""
    days = 1
    if context.args:
        arg = context.args[0].lower().replace("d", "").strip()
        if arg.isdigit():
            days = max(1, min(int(arg), 365))

    data, err = await asyncio.to_thread(_fetch_account_data, days)
    if err:
        await update.effective_message.reply_text(err, parse_mode="Markdown")
        return

    curr = data['currency']
    float_pnl = data['profit']
    pnl_sign = "+" if float_pnl > 0 else ""
    pnl_icon = "📈" if float_pnl > 0 else ("📉" if float_pnl < 0 else "⚖️")

    closed = data['closed_deals']
    closed_pnl = sum(d['net_pnl'] for d in closed)
    closed_sign = "+" if closed_pnl > 0 else ""
    wins = sum(1 for d in closed if d['net_pnl'] > 0)
    losses = sum(1 for d in closed if d['net_pnl'] < 0)
    total_closed = len(closed)
    win_rate = round((wins / total_closed * 100), 1) if total_closed > 0 else 0.0

    period_str = "Today (24h)" if days == 1 else f"Last {days} Days"

    # Format recent closed deals (up to 5)
    history_lines = []
    for d in sorted(closed, key=lambda x: x['time'], reverse=True)[:5]:
        t_str = d['time'].strftime('%Y-%m-%d %H:%M')
        d_pnl_sign = "+" if d['net_pnl'] > 0 else ""
        d_icon = "🎯" if d['net_pnl'] > 0 else "🛡️"
        history_lines.append(
            f"• `{t_str}` | **{d['symbol']} {d['type']}** `{d['volume']}` lots {d_icon}\n"
            f"  └ Exit: `${d['price']:.2f}` | PnL: `{d_pnl_sign}${d['net_pnl']:.2f} {curr}`"
        )

    history_text = "\n".join(history_lines) if history_lines else "_No closed deals in this timeframe._"

    text = (
        f"💳 **ACCOUNT & PNL OVERVIEW**\n\n"
        f"**👤 Account Info**\n"
        f"• **Account:** `{data['login']}` (`{data['server']}`)\n"
        f"• **Balance:** `${data['balance']:,.2f} {curr}`\n"
        f"• **Equity:** `${data['equity']:,.2f} {curr}`\n"
        f"• **Floating PnL:** `{pnl_sign}${float_pnl:,.2f} {curr}` {pnl_icon}\n"
        f"• **Margin:** `${data['margin']:,.2f}` | **Free Margin:** `${data['free_margin']:,.2f}`\n"
        f"• **Margin Level:** `{data['margin_level']:.1f}%` | **Leverage:** `1:{data['leverage']}`\n\n"
        f"**📊 Realized PnL ({period_str})**\n"
        f"• **Closed PnL:** `{closed_sign}${closed_pnl:,.2f} {curr}`\n"
        f"• **Record:** `{wins}W - {losses}L` (`{total_closed}` trades)\n"
        f"• **Win Rate:** `{win_rate}%`\n\n"
        f"**📜 Recent Closed History**\n"
        f"{history_text}\n\n"
        f"💡 _Tip: Use `/account 7d` or `/account 30d` for longer history lookup._"
    )

    await update.effective_message.reply_text(text, parse_mode="Markdown")


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
    skipped = counts.get('SKIPPED', 0)
    total_signals = sum(v for k, v in counts.items() if k != 'SKIPPED')
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
        f"• **Total Signals:** `{total_signals}` | **Pending Entry:** `{pending}` | **Skipped (alert-only):** `{skipped}`\n"
        f"• **Dual-Entry Runners (leg2 in flight):** `{running}` 🎯 *(open — not counted below)*\n"
        f"• **Single-Entry TP1 Wins:** `{closed_tp1}` 🎯\n"
        f"• **TP2 Hits (leg2):** `{tp2}` 🚀\n"
        f"• **Break-Even Closes (leg2):** `{be}` 🔒\n"
        f"• **Stop Loss Hits:** `{sl}` 🛡️\n\n"
        f"📈 **Win Rate:** `{win_rate}%` (`{wins}/{total_closed}` closed trades)"
    )
    await update.effective_message.reply_text(reply, parse_mode="Markdown")