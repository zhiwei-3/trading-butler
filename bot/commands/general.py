# bot/commands/general.py
import asyncio
from datetime import datetime, timedelta, time as dtime

from telegram import Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, save_settings
from core.account import HISTORY_START, fetch_account_data
from core.database import get_signal_stats
from core.stats import deal_stats, signal_summary, realized_return_pct
from bot.jobs.status import build_status_snapshot
from bot.registry import command, callback, restart_job, stop_job, help_text
from utils.fmt import money


def _parse_period(args):
    """
    /account, /positions, or /journal -> all time
    today|week|month|all
    7d | 7 -> rolling N days (1..365)
    "Today" starts at 00:00 in the PC's local timezone.
    """
    now = datetime.now().astimezone()
    midnight = datetime.combine(now.date(), dtime.min).astimezone()   # local midnight (DST-correct)
    if not args:
        return HISTORY_START, "All Time", True

    arg = args[0].lower().strip()
    if arg in ("today", "1d"):
        return midnight, "Today", False
    if arg in ("week", "7d"):
        return now - timedelta(days=7), "Last 7 Days", False
    if arg in ("month", "30d"):
        return now - timedelta(days=30), "Last 30 Days", False
    if arg in ("all", "alltime", "all-time", "max"):
        return HISTORY_START, "All Time", True

    digits = arg[:-1] if arg.endswith("d") else arg
    if digits.isdigit():
        days = max(1, min(int(digits), 365))
        return now - timedelta(days=days), f"Last {days} Day{'s' if days != 1 else ''}", False
    return None, None, False


@command("start", "Show Quick Command Guide", icon="🤵‍♂️", group="Core & Dashboards", order=0, listed=False)
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text(), parse_mode="Markdown")


@command("account", "Executive Capital & Margin Health", icon="💳", group="Core & Dashboards", usage="/account [period]", order=2)
@callback("btn_account")
async def account_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executive view of capital, margin, deposits/withdrawals, net realized PnL, and signal engine stats."""
    since_utc, period_str, _is_all = _parse_period(context.args)
    if since_utc is None:
        await update.effective_message.reply_text(
            "⚠️ **Usage:** `/account [period]`\n\n"
            "• `/account` — all time\n"
            "• `/account today` · `/account week` · `/account month` · `/account 14d`",
            parse_mode="Markdown"
        )
        return

    data, err = await asyncio.to_thread(fetch_account_data)
    if err:
        await update.effective_message.reply_text(err, parse_mode="Markdown")
        return
    counts = await asyncio.to_thread(get_signal_stats)

    curr = data["currency"]
    balance, equity, float_pnl = data["balance"], data["equity"], data["profit"]
    float_pct = (float_pnl / balance * 100) if balance else 0.0
    pnl_icon = "📈" if float_pnl > 0 else ("📉" if float_pnl < 0 else "⚖️")

    st = deal_stats([t for t in data["trades"] if t["time"] >= since_utc])
    sig = signal_summary(counts)
    ret = realized_return_pct(equity, data["deposits"], data["withdrawals"])
    ret_str = f"{ret:+.2f}%" if ret is not None else "N/A"

    text = (
        f"💳 **ACCOUNT & CAPITAL SNAPSHOT**\n\n"
        f"**👤 Account Details**\n"
        f"• **Account:** `{data['login']}` (`{data['server']}`) | **Leverage:** `1:{data['leverage']}`\n"
        f"• **Balance:** `${balance:,.2f} {curr}` | **Equity:** `${equity:,.2f} {curr}`\n"
        f"• **Floating PnL:** `{money(float_pnl, curr)}` (`{float_pct:+.2f}%`) {pnl_icon}\n"
        f"• **Margin:** `${data['margin']:,.2f}` | **Free Margin:** `${data['free_margin']:,.2f}` | "
        f"**Margin Level:** `{data['margin_level']:.1f}%`\n\n"
        f"**📊 Capital Summary ({period_str})**\n"
        f"• **Total Deposited:** `${data['deposits']:,.2f}` | **Withdrawn:** `${data['withdrawals']:,.2f}`\n"
        f"• **Net Realized PnL:** `{money(st['pnl'], curr)}` ({st['wins']}W - {st['losses']}L)\n"
        f"• **Account Realized Return:** `{ret_str}`\n\n"
        f"**🧪 Bot Signals (Forward-Test Engine)**\n"
        f"• **Total Signals:** `{sig['total']}` | **Pending:** `{sig['pending']}` | "
        f"**Runners Active:** `{sig['running']}` | **Alert-Only:** `{sig['skipped']}`\n"
        f"• 🎯 TP1: `{sig['tp1']}` | 🚀 TP2: `{sig['tp2']}` | 🔒 BE: `{sig['be']}` | 🛡️ SL: `{sig['sl']}`\n"
        f"• **Forward Win Rate:** `{sig['win_rate']}%` (`{sig['wins']}/{sig['closed']}` closed signals)\n\n"
        f"💡 _Tip: Use `/positions` for open trades and `/journal` for deal history & analytics._"
    )

    await update.effective_message.reply_text(text, parse_mode="Markdown")


@command("scanner_on", "Activate Market Scanner", icon="🟢", group="Core & Dashboards", order=30)
@callback("btn_scanner_on")
async def enable_scanner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ALERT_STATE["scanner_enabled"] = True
    save_settings()
    restart_job(context.job_queue, "xauusd_scanner", update.effective_chat.id)
    await update.effective_message.reply_text("🟢 **Market Scanner Activated!** Checking XAUUSD every 60 seconds.", parse_mode="Markdown")


@command("scanner_off", "Deactivate Market Scanner", icon="🔴", group="Core & Dashboards", order=31)
@callback("btn_scanner_off")
async def disable_scanner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ALERT_STATE["scanner_enabled"] = False
    save_settings()
    stop_job(context.job_queue, "xauusd_scanner")
    await update.effective_message.reply_text("🔴 **Market Scanner Deactivated.**", parse_mode="Markdown")


@command("status", "Full Bot Health Check", icon="🩺", group="Core & Dashboards", order=5)
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    snapshot = await build_status_snapshot()
    await update.message.reply_text(f"🩺 **BOT STATUS CHECK**\n\n{snapshot}", parse_mode="Markdown")


@command("heartbeat", "Adjust Periodic Status Pings", icon="💓", group="Testing & Utility",
         usage="/heartbeat on|off|test", order=30)
async def heartbeat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    chat_id = update.effective_chat.id
    if not args:
        status = "🟢 ON" if ALERT_STATE["heartbeat_enabled"] else "🔴 OFF"
        await update.message.reply_text(f"💓 **HEARTBEAT:** {status} every `{ALERT_STATE['heartbeat_interval_hours']}h`\n\n**Usage:** `/heartbeat on|off|test`", parse_mode="Markdown")
        return
    sub = args[0].lower()
    if sub == "test":
        snapshot = await build_status_snapshot()
        await update.message.reply_text(f"💓 **TEST HEARTBEAT**\n\n{snapshot}", parse_mode="Markdown")
    elif sub in ("on", "off"):
        ALERT_STATE["heartbeat_enabled"] = (sub == "on")
        save_settings()
        restart_job(context.job_queue, "heartbeat_ping", chat_id)     # starts only if enabled
        await update.message.reply_text(f"{'✅' if sub == 'on' else '🔴'} Heartbeat **{sub.upper()}**", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ **Unknown option.** Usage: `/heartbeat on|off|test`", parse_mode="Markdown")