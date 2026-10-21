# bot/commands/trading.py

import asyncio
import MetaTrader5 as mt5
from telegram import Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, save_settings
from trade_engine import (
    list_managed_positions, close_position, close_all_managed,
    modify_position_sltp, pending_intent_count,
)
from ._common import admin_only
from .core import _fetch_account_data, _deal_stats, _parse_period, _money, _pf_text


@admin_only
async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Arms/disarms live execution. Going LIVE requires an explicit confirmation token."""
    args = [a.lower() for a in (context.args or [])]

    if not args:
        armed = ALERT_STATE.get("auto_trade_enabled", False)
        dry = ALERT_STATE.get("trade_dry_run", True)
        mode = "🔴 DISARMED" if not armed else ("🧪 ARMED (DRY-RUN)" if dry else "🟢 ARMED (LIVE MONEY)")
        await update.effective_message.reply_text(
            "🤖 **TRADE EXECUTION ENGINE**\n\n"
            f"• **Status:** `{mode}`\n"
            f"• **Magic Number:** `{ALERT_STATE.get('magic_number')}`\n"
            f"• **Sizing:** `{ALERT_STATE.get('fixed_lot_size')}` lots per leg (fixed — not scaled by account size)\n"
            f"• **Dual-Entry Threshold:** SMC Confluence score `≥ {ALERT_STATE.get('dual_entry_score_threshold')}` "
            f"→ 2 legs (TP1 full close + TP2/BE runner). Below that, or any other strategy, → 1 leg (TP1-only).\n"
            f"• **Max Trades / Day:** `{ALERT_STATE.get('max_daily_trades')}` *(counts tickets, so a dual entry uses 2)*\n"
            f"• **Slippage Allowance:** `{ALERT_STATE.get('max_slippage_points')}` points\n"
            f"• **Entry Drift Abort:** `{ALERT_STATE.get('max_entry_drift_pct')}%` of SL\n"
            f"• **Flatten on Circuit Breaker:** {'ON ✅' if ALERT_STATE.get('flatten_on_circuit_breaker') else 'OFF ❌'}\n"
            f"• **Queued Intents:** `{pending_intent_count()}`\n\n"
            "**Usage:**\n"
            "• `/trade on` — arm in dry-run (safe)\n"
            "• `/trade off` — disarm entirely\n"
            "• `/trade dry` — back to dry-run\n"
            "• `/trade live CONFIRM` — ⚠️ send **real orders**\n"
            "• `/positions` · `/close <ticket|all>`",
            parse_mode="Markdown"
        )
        return

    sub = args[0]
    if sub == "on":
        ALERT_STATE["auto_trade_enabled"] = True
        ALERT_STATE["trade_dry_run"] = True
        save_settings()
        await update.effective_message.reply_text(
            "🧪 **Auto-trade ARMED in DRY-RUN.** Orders will be built and logged but never sent.\n"
            "Watch a few signals, then `/trade live CONFIRM`.", parse_mode="Markdown")
    elif sub == "off":
        ALERT_STATE["auto_trade_enabled"] = False
        save_settings()
        await update.effective_message.reply_text(
            "🔴 **Auto-trade DISARMED.** Open positions are untouched — use `/close all` to flatten.",
            parse_mode="Markdown")
    elif sub == "dry":
        ALERT_STATE["trade_dry_run"] = True
        save_settings()
        await update.effective_message.reply_text("🧪 **Dry-run re-enabled.** No further orders will be sent.", parse_mode="Markdown")
    elif sub == "live":
        if len(args) < 2 or context.args[1] != "CONFIRM":
            await update.effective_message.reply_text(
                "⚠️ **This sends real orders with real money.**\n\n"
                "Confirm with exactly: `/trade live CONFIRM`", parse_mode="Markdown")
            return
        ALERT_STATE["auto_trade_enabled"] = True
        ALERT_STATE["trade_dry_run"] = False
        save_settings()
        await update.effective_message.reply_text(
            "🟢 **LIVE EXECUTION ENABLED.**\n\n"
            f"• Fixed `{ALERT_STATE.get('fixed_lot_size')}` lots per leg\n"
            f"• Dual-entry (2 legs) above SMC Confluence score `{ALERT_STATE.get('dual_entry_score_threshold')}`\n"
            f"• Max `{ALERT_STATE.get('max_daily_trades')}` fills/day (one trade idea open at a time)\n"
            f"• Circuit breaker: `{ALERT_STATE.get('max_daily_losses')}` losses / "
            f"`-{ALERT_STATE.get('max_daily_drawdown_r')}R`\n\n"
            "🧯 Kill switch: `/trade off` then `/close all`.\n"
            "*Confirm AutoTrading is enabled in the MT5 terminal.*", parse_mode="Markdown")
    else:
        await update.effective_message.reply_text("⚠️ Unknown option. Send `/trade` for usage.", parse_mode="Markdown")


@admin_only
async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trade Desk & Journal: Active open positions, historical closed deals, win/loss analytics."""
    since_utc, period_str, is_all = _parse_period(context.args)
    if since_utc is None:
        await update.effective_message.reply_text(
            "⚠️ **Usage:** `/positions [period]`\n\n"
            "• `/positions` — today (since 00:00 UTC)\n"
            "• `/positions week` · `/positions month` · `/positions 14d` · `/positions all`",
            parse_mode="Markdown"
        )
        return

    data, err = await asyncio.to_thread(_fetch_account_data)
    if err:
        await update.effective_message.reply_text(err, parse_mode="Markdown")
        return

    curr = data["currency"]
    positions = data["open_positions"]
    all_trades = data["trades"]
    period_trades = [t for t in all_trades if t["time"] >= since_utc]
    st = _deal_stats(period_trades)

    filter_mode = "ALL"
    if len(context.args) > 1:
        f_arg = context.args[1].lower()
        if f_arg == "bot":
            period_trades = [t for t in period_trades if t["is_bot"]]
            st = _deal_stats(period_trades)
            filter_mode = "BOT"
        elif f_arg in ("manual", "man"):
            period_trades = [t for t in period_trades if not t["is_bot"]]
            st = _deal_stats(period_trades)
            filter_mode = "MANUAL"

    pos_lines = []
    if not positions:
        pos_text = "_No open positions currently._\n"
    else:
        total_float = sum(p.profit for p in positions)
        for p in positions:
            side = "BUY 🟢" if p.type == mt5.POSITION_TYPE_BUY else "SELL 🔴"
            pos_lines.append(
                f"• `#{p.ticket}` {side} `{p.volume}` lots **{p.symbol}** — *{p.comment or ''}*\n"
                f"   Entry `${p.price_open:.2f}` ➔ Now `${p.price_current:.2f}` | "
                f"SL `${p.sl:.2f}` | TP `${p.tp:.2f}` | PnL: `{_money(p.profit, curr)}`"
            )
        pos_lines.append(f"   └ **Total Floating PnL:** `{_money(total_float, curr)}`")
        pos_text = "\n".join(pos_lines) + "\n"

    bot_st = _deal_stats([t for t in period_trades if t["is_bot"]])
    man_st = _deal_stats([t for t in period_trades if not t["is_bot"]])

    analytics_text = (
        f"• **Net Realized PnL:** `{_money(st['pnl'], curr)}` | `{st['wins']}W - {st['losses']}L` (`{st['n']}` deals)\n"
        f"• **Win Rate:** `{st['win_rate']}%` | **Profit Factor:** `{_pf_text(st)}` | **Expectancy:** `{st['expectancy']:+,.2f}`/deal\n"
        f"• **Avg Win:** `{st['avg_win']:+,.2f}` | **Avg Loss:** `{st['avg_loss']:+,.2f}` | "
        f"**Best:** `{st['best']:+,.2f}` | **Worst:** `{st['worst']:+,.2f}`\n"
        f"• **Max Drawdown:** `-{st['max_dd']:,.2f} {curr}` | **Streaks:** `{st['max_w']}`W / `{st['max_l']}`L\n"
        f"• 🤖 Bot: `{_money(bot_st['pnl'], curr)}` ({bot_st['n']} deals, {bot_st['win_rate']}% WR) | "
        f"🖐️ Manual: `{_money(man_st['pnl'], curr)}` ({man_st['n']} deals, {man_st['win_rate']}% WR)\n"
    )

    recent_lines = []
    for t in sorted(period_trades, key=lambda x: x["time"], reverse=True)[:5]:
        icon = "🎯" if t["net_pnl"] > 0 else ("🛡️" if t["net_pnl"] < 0 else "⚖️")
        src = "🤖" if t["is_bot"] else "🖐️"
        recent_lines.append(
            f"• `{t['time'].strftime('%m-%d %H:%M')} UTC` {src} **{t['symbol']} {t['type']}** "
            f"`{t['volume']}` lots {icon} `{_money(t['net_pnl'], curr)}`"
        )
    recent_text = "\n".join(recent_lines) if recent_lines else "_No closed deals in this window._"

    text = (
        f"📑 **POSITIONS & TRADE JOURNAL ({period_str})**" + (f" `[{filter_mode}]`\n\n" if filter_mode != "ALL" else "\n\n") +
        f"**💼 Active Managed Positions ({len(positions)})**\n"
        f"{pos_text}\n"
        f"**📊 Trade Execution Analytics**\n"
        f"{analytics_text}\n"
        f"**📜 Recent Closed Deals**\n"
        f"{recent_text}\n\n"
        f"💡 _Filter with `/positions week`, `/positions month`, `/positions all`, `/positions week bot`, or `/positions month manual`._"
    )

    await update.effective_message.reply_text(text, parse_mode="Markdown")


@admin_only
async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.effective_message.reply_text(
            "⚠️ **Usage:** `/close <ticket>` · `/close all` · `/close <ticket> <lots>`",
            parse_mode="Markdown")
        return

    if args[0].lower() == "all":
        results = await asyncio.to_thread(close_all_managed)
        await update.effective_message.reply_text(
            "🧯 **FLATTEN MANAGED POSITIONS**\n\n" + "\n".join(f"• {r}" for r in results),
            parse_mode="Markdown")
        return

    try:
        ticket = int(args[0])
        lots = float(args[1]) if len(args) > 1 else None
    except ValueError:
        await update.effective_message.reply_text("❌ Ticket must be numeric.", parse_mode="Markdown")
        return

    res = await asyncio.to_thread(close_position, ticket, lots)
    icon = "✅" if res["ok"] else "❌"
    await update.effective_message.reply_text(
        f"{icon} **Close `#{ticket}`** — {res['reason']}", parse_mode="Markdown")


@admin_only
async def breakeven_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trail a managed position's SL to its entry price."""
    if not context.args:
        await update.effective_message.reply_text("⚠️ **Usage:** `/breakeven <ticket>`", parse_mode="Markdown")
        return
    try:
        ticket = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Ticket must be numeric.", parse_mode="Markdown")
        return

    positions = await asyncio.to_thread(list_managed_positions)
    pos = next((p for p in positions if p.ticket == ticket), None)
    if pos is None:
        await update.effective_message.reply_text(f"❌ No managed position `#{ticket}`.", parse_mode="Markdown")
        return

    res = await asyncio.to_thread(modify_position_sltp, ticket, pos.price_open, pos.tp or None)
    icon = "✅" if res["ok"] else "❌"
    await update.effective_message.reply_text(
        f"{icon} **`#{ticket}` SL → break-even** (`${pos.price_open:.2f}`) — {res['reason']}",
        parse_mode="Markdown")