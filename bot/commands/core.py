# bot/commands/core.py

import logging
import asyncio
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5
from telegram.error import BadRequest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import USER_ID, ALERT_STATE, save_settings
from database import get_signal_stats
from mt5_engine import check_mt5_alive, MT5_LOCK, get_broker_utc_offset_seconds
from bot.jobs import (
    build_status_snapshot,
    market_scanner_job,
    ensure_watchdog_running,
    restart_heartbeat_job,
)
from ._common import admin_only


_HISTORY_START = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _parse_period(args):
    """
    /account, /positions, or /journal -> today (since 00:00 UTC)
    today|week|month|all
    7d | 7 -> rolling N days (1..365)
    """
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if not args:
        return midnight, "Today (UTC)", False

    arg = args[0].lower().strip()
    if arg in ("today", "1d"):
        return midnight, "Today (UTC)", False
    if arg in ("week", "7d"):
        return now - timedelta(days=7), "Last 7 Days", False
    if arg in ("month", "30d"):
        return now - timedelta(days=30), "Last 30 Days", False
    if arg in ("all", "alltime", "all-time", "max"):
        return _HISTORY_START, "All Time", True

    digits = arg[:-1] if arg.endswith("d") else arg
    if digits.isdigit():
        days = max(1, min(int(digits), 365))
        return now - timedelta(days=days), f"Last {days} Day{'s' if days != 1 else ''}", False
    return None, None, False


def _fetch_account_data():
    """BLOCKING — call via asyncio.to_thread. Fetches account info, positions, and history deals."""
    if not check_mt5_alive():
        return None, "🔴 MT5 terminal is not connected or unreachable."

    offset_s = int(get_broker_utc_offset_seconds())
    shift = timedelta(seconds=offset_s)
    now = datetime.now(timezone.utc)
    magic = int(ALERT_STATE.get("magic_number", 770077))

    with MT5_LOCK:
        info = mt5.account_info()
        if info is None:
            return None, "❌ Failed to retrieve MT5 account details."
        deals = mt5.history_deals_get(_HISTORY_START + shift, now + shift + timedelta(days=1))
        positions = mt5.positions_get()

    trades, deposits, withdrawals = [], 0.0, 0.0
    for d in (deals or []):
        if d.type == mt5.DEAL_TYPE_BALANCE:
            if d.profit > 0:
                deposits += d.profit
            else:
                withdrawals += -d.profit
            continue
        if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT, mt5.DEAL_ENTRY_OUT_BY) \
                and d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
            trades.append({
                "ticket": d.ticket,
                "time": datetime.fromtimestamp(d.time - offset_s, tz=timezone.utc),
                "symbol": d.symbol,
                "type": "SELL" if d.type == mt5.DEAL_TYPE_BUY else "BUY",
                "volume": d.volume,
                "price": d.price,
                "net_pnl": d.profit + d.swap + d.commission,
                "is_bot": d.magic == magic,
            })
    trades.sort(key=lambda t: t["time"])

    open_all = list(positions or [])
    open_bot = [p for p in open_all if p.magic == magic]

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
        "deposits": deposits,
        "withdrawals": withdrawals,
        "trades": trades,
        "open_positions": open_all,
        "open_total": len(open_all),
        "open_bot": len(open_bot),
        "float_bot": sum(p.profit for p in open_bot),
        "float_manual": sum(p.profit for p in open_all if p.magic != magic),
    }, None


def _deal_stats(trades):
    """Win/loss, profit factor, expectancy, realized drawdown and streaks."""
    pnls = [t["net_pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    decided = len(wins) + len(losses)

    cum = peak = max_dd = 0.0
    cur_w = cur_l = max_w = max_l = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        if p > 0:
            cur_w, cur_l = cur_w + 1, 0
        elif p < 0:
            cur_l, cur_w = cur_l + 1, 0
        else:
            cur_w = cur_l = 0
        max_w, max_l = max(max_w, cur_w), max(max_l, cur_l)

    return {
        "n": len(pnls), "pnl": sum(pnls),
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / decided * 100, 1) if decided else 0.0,
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "expectancy": sum(pnls) / len(pnls) if pnls else 0.0,
        "best": max(pnls, default=0.0), "worst": min(pnls, default=0.0),
        "max_dd": max_dd, "max_w": max_w, "max_l": max_l,
    }


def _money(v, curr):
    return f"{v:+,.2f} {curr}"


def _pf_text(st):
    if st["pf"] is not None:
        return f"{st['pf']}"
    return "∞" if st["wins"] else "—"


@admin_only
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_watchdog_running(context.job_queue, chat_id)

    await update.message.reply_text(
        "🤵‍♂️ **Trading Butler Online (Full Stack Modular)**\n\n"
        "**🎛️ Core & Dashboards**\n"
        "• `/menu` - Interactive Control Dashboard\n"
        "• `/account [period]` - Executive Capital & Margin Health Snapshot\n"
        "• `/scanner_on` / `/scanner_off` - Toggle Market Scanner\n"
        "• `/status` - Full Bot Health Check\n\n"
        "**🤖 Trade Execution & History**\n"
        "• `/trade` - Arm/Disarm Live Execution (Dry/Live)\n"
        "• `/positions` - Open Running Positions (Live + Dry-Run)\n"
        "• `/journal [period]` - Interactive Trade Journal & Closed Deal Analytics\n"
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
            InlineKeyboardButton("💼 Positions", callback_data="btn_positions"),
            InlineKeyboardButton("📓 Journal", callback_data="btn_journal"),
        ],
        [
            InlineKeyboardButton("📊 Gold Chart", callback_data="btn_gold"),
            InlineKeyboardButton("⚙️ Strategy Selector", callback_data="btn_strat_menu"),
            InlineKeyboardButton("🕒 Timeframe Selector", callback_data="btn_tf_menu"),
        ],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


@admin_only
async def account_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executive view of capital, margin, deposits/withdrawals, net realized PnL, and signal engine stats."""
    since_utc, period_str, is_all = _parse_period(context.args)
    if since_utc is None:
        await update.effective_message.reply_text(
            "⚠️ **Usage:** `/account [period]`\n\n"
            "• `/account` — today (since 00:00 UTC)\n"
            "• `/account week` · `/account month` · `/account 14d` · `/account all`",
            parse_mode="Markdown"
        )
        return

    data, err = await asyncio.to_thread(_fetch_account_data)
    if err:
        await update.effective_message.reply_text(err, parse_mode="Markdown")
        return
    counts = await asyncio.to_thread(get_signal_stats)

    curr = data["currency"]
    balance, equity, float_pnl = data["balance"], data["equity"], data["profit"]
    float_pct = (float_pnl / balance * 100) if balance else 0.0
    pnl_icon = "📈" if float_pnl > 0 else ("📉" if float_pnl < 0 else "⚖️")

    all_trades = data["trades"]
    period_trades = [t for t in all_trades if t["time"] >= since_utc]
    st_period = _deal_stats(period_trades)

    net_dep = data["deposits"] - data["withdrawals"]
    ret_str = f"{(equity - net_dep) / net_dep * 100:+.2f}%" if net_dep > 0 else "N/A"

    skipped = counts.get("SKIPPED", 0)
    total_sig = sum(v for k, v in counts.items() if k != "SKIPPED")
    pending_sig = counts.get("PENDING", 0)
    running_sig = counts.get("HIT_TP1", 0)
    tp1_sig = counts.get("CLOSED_TP1", 0)
    tp2_sig = counts.get("HIT_TP2", 0)
    be_sig = counts.get("CLOSED_BE", 0)
    sl_sig = counts.get("HIT_SL", 0)
    wins_sig = tp1_sig + tp2_sig + be_sig
    closed_sig = wins_sig + sl_sig
    wr_sig = round(wins_sig / closed_sig * 100, 1) if closed_sig else 0.0

    text = (
        f"💳 **ACCOUNT & CAPITAL SNAPSHOT**\n\n"
        f"**👤 Account Details**\n"
        f"• **Account:** `{data['login']}` (`{data['server']}`) | **Leverage:** `1:{data['leverage']}`\n"
        f"• **Balance:** `${balance:,.2f} {curr}` | **Equity:** `${equity:,.2f} {curr}`\n"
        f"• **Floating PnL:** `{_money(float_pnl, curr)}` (`{float_pct:+.2f}%`) {pnl_icon}\n"
        f"• **Margin:** `${data['margin']:,.2f}` | **Free Margin:** `${data['free_margin']:,.2f}` | "
        f"**Margin Level:** `{data['margin_level']:.1f}%`\n\n"
        f"**📊 Capital Summary ({period_str})**\n"
        f"• **Total Deposited:** `${data['deposits']:,.2f}` | **Withdrawn:** `${data['withdrawals']:,.2f}`\n"
        f"• **Net Realized PnL:** `{_money(st_period['pnl'], curr)}` ({st_period['wins']}W - {st_period['losses']}L)\n"
        f"• **Account Realized Return:** `{ret_str}`\n\n"
        f"**🧪 Bot Signals (Forward-Test Engine)**\n"
        f"• **Total Signals:** `{total_sig}` | **Pending:** `{pending_sig}` | **Runners Active:** `{running_sig}` | **Alert-Only:** `{skipped}`\n"
        f"• 🎯 TP1: `{tp1_sig}` | 🚀 TP2: `{tp2_sig}` | 🔒 BE: `{be_sig}` | 🛡️ SL: `{sl_sig}`\n"
        f"• **Forward Win Rate:** `{wr_sig}%` (`{wins_sig}/{closed_sig}` closed signals)\n\n"
        f"💡 _Tip: Use `/positions` for open trades and `/journal` for deal history & analytics._"
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
