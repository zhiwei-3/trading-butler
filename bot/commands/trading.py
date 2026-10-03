import asyncio
import MetaTrader5 as mt5
from telegram import Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, save_settings
from mt5_engine import get_gold_symbol, calculate_position_size
from trade_engine import (
    list_managed_positions, close_position, close_all_managed,
    modify_position_sltp, pending_intent_count,
)
from ._common import admin_only


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
            f"• **Max Open Positions:** `{ALERT_STATE.get('max_open_positions')}` *(the 2nd leg of one signal doesn't count against this)*\n"
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
            f"• Max `{ALERT_STATE.get('max_daily_trades')}` fills/day, "
            f"`{ALERT_STATE.get('max_open_positions')}` open at once\n"
            f"• Circuit breaker: `{ALERT_STATE.get('max_daily_losses')}` losses / "
            f"`-{ALERT_STATE.get('max_daily_drawdown_r')}R`\n\n"
            "🧯 Kill switch: `/trade off` then `/close all`.\n"
            "*Confirm AutoTrading is enabled in the MT5 terminal.*", parse_mode="Markdown")
    else:
        await update.effective_message.reply_text("⚠️ Unknown option. Send `/trade` for usage.", parse_mode="Markdown")


@admin_only
async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    positions = await asyncio.to_thread(list_managed_positions)
    if not positions:
        await update.effective_message.reply_text("📭 **No bot-managed positions open.**", parse_mode="Markdown")
        return

    lines = ["💼 **OPEN MANAGED POSITIONS**\n"]
    total = 0.0
    for p in positions:
        side = "BUY 🟢" if p.type == mt5.POSITION_TYPE_BUY else "SELL 🔴"
        total += p.profit
        lines.append(
            f"• `#{p.ticket}` {side} `{p.volume}` lots {p.symbol} — *{p.comment or ''}*\n"
            f"   Entry `${p.price_open:.2f}` → Now `${p.price_current:.2f}`\n"
            f"   SL `${p.sl:.2f}` | TP `${p.tp:.2f}` | P&L `${p.profit:.2f}`"
        )
    lines.append(f"\n📊 **Floating P&L:** `${total:.2f}`")
    lines.append("\n*Close with* `/close <ticket>` *or* `/close all`.")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


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
    """Manually trail a managed position's SL to its entry price. The bot does
    this automatically for a dual-entry signal's runner leg — this is for
    manual overrides."""
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


@admin_only
async def calc_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual, standalone reference calculator — independent of the bot's own
    fixed-lot auto-trade sizing (see /trade)."""
    try:
        args = context.args
        if len(args) < 3:
            await update.message.reply_text("⚠️ **Usage:** `/calc <balance> <risk_pct> <sl_pips>`", parse_mode="Markdown")
            return

        balance, risk_pct, sl_pips = float(args[0]), float(args[1]), float(args[2])
        if sl_pips <= 0:
            await update.message.reply_text("❌ Stop loss pips must be greater than 0.", parse_mode="Markdown")
            return

        symbol = await asyncio.to_thread(get_gold_symbol) or "XAUUSD"

        # Convert pips to absolute price distance (XAUUSD 1 pip = 0.1 price move).
        # Adjust the multiplier if your broker formats gold points differently.
        sl_dist = sl_pips * 0.1

        pos = await asyncio.to_thread(calculate_position_size, symbol, sl_dist, risk_pct)

        reply = (
            f"🧮 **POSITION RISK CALCULATOR ({symbol})** — *manual reference only*\n\n"
            f"• **Account Balance:** `${pos['balance']:,.2f}`\n"
            f"• **Risk Target ({risk_pct}%):** `${pos['risk_usd']:,.2f}`\n"
            f"• **Stop Loss Distance:** `{sl_pips} pips`\n\n"
            f"🎯 **Risk-Based Lot Size:** `{pos['lots']}` Lots\n\n"
            f"ℹ️ *The bot's own auto-trade execution uses a fixed `{ALERT_STATE.get('fixed_lot_size')}` "
            f"lots/leg regardless of this calculation — see `/trade`.*"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid numerical values.")
    except Exception as e:
        await update.message.reply_text(f"❌ Calculation error: {e}")