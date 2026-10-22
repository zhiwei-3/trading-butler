# bot/commands/trading.py

import math
import asyncio
import MetaTrader5 as mt5
from telegram import Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, save_settings
from mt5_engine import check_mt5_alive, get_tick, MT5_LOCK
from database import get_open_dry_run_signals
from trade_engine import (
    list_managed_positions, close_position, close_all_managed,
    modify_position_sltp, pending_intent_count,
)
from ._common import admin_only, md_safe
from .core import _money

_POSITIONS_USAGE = (
    "⚠️ **Usage:** `/position [all|dry|live|bot|manual]`\n\n"
    "• `/position` — all open & running positions (dry-run & live)\n"
    "• `/position dry` — show only running dry-run positions\n"
    "• `/position live` — show only running live MT5 positions\n"
    "• `/position bot` — show bot positions (live + dry-run)\n"
    "• `/position manual` — show manual MT5 positions\n"
    "• Aliases: `/position` or `/positions`"
)
_MAX_POSITION_ROWS = 12


def _calc_pnl(symbol, direction, volume, price_open, price_current):
    if price_open is None or price_current is None or not volume:
        return 0.0
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    try:
        with MT5_LOCK:
            profit = mt5.order_calc_profit(order_type, symbol, float(volume), float(price_open), float(price_current))
        if profit is not None:
            return round(float(profit), 2)
    except Exception:
        pass

    try:
        contract_size = 100.0
        with MT5_LOCK:
            info = mt5.symbol_info(symbol)
            if info and info.trade_contract_size:
                contract_size = float(info.trade_contract_size)
        diff = (price_current - price_open) if direction == "BUY" else (price_open - price_current)
        return round(diff * float(volume) * contract_size, 2)
    except Exception:
        diff = (price_current - price_open) if direction == "BUY" else (price_open - price_current)
        return round(diff * float(volume) * 100.0, 2)


def _fetch_live_positions():
    """BLOCKING. Returns (currency, list_of_open_positions, error_message)."""
    if not check_mt5_alive():
        return "USD", [], "🔴 MT5 terminal is not connected or unreachable."
    with MT5_LOCK:
        info = mt5.account_info()
        curr = info.currency if info and info.currency else "USD"
        positions = mt5.positions_get()
        if positions is None:
            err = mt5.last_error()
            if err and err[0] == getattr(mt5, "RES_S_OK", 1):
                positions = []
            else:
                return curr, [], f"MT5 positions query failed ({err})."
    return curr, list(positions or []), None


def _fetch_dry_run_positions(curr="USD"):
    signals = get_open_dry_run_signals()
    if not signals:
        return []

    fixed_lot = float(ALERT_STATE.get("fixed_lot_size", 0.01))
    ticks = {}
    positions = []

    for sig in signals:
        symbol = sig["symbol"] or "XAUUSD"
        direction = sig["direction"]
        status = sig["status"]
        dual = bool(sig["dual_entry"])

        if symbol not in ticks:
            try:
                ticks[symbol] = get_tick(symbol)
            except Exception:
                ticks[symbol] = None
        tick = ticks.get(symbol)

        live_price = None
        if tick is not None:
            live_price = float(tick.bid if direction == "BUY" else tick.ask)

        # Leg 1: active only when status is PENDING and not a live ticket
        if status == "PENDING" and not (sig.get("exec_mode") == "LIVE" and sig.get("ticket")):
            entry = float(sig["fill_price"]) if sig.get("fill_price") else float(sig["entry_price"])
            lots = float(sig["lots"]) if sig.get("lots") else fixed_lot
            sl = float(sig["sl_price"])
            tp = float(sig["tp1_price"])
            pnl = _calc_pnl(symbol, direction, lots, entry, live_price) if live_price is not None else 0.0
            positions.append({
                "signal_id": sig["id"],
                "leg": 1,
                "label": "TP1",
                "symbol": symbol,
                "direction": direction,
                "volume": lots,
                "price_open": entry,
                "sl": sl,
                "tp": tp,
                "price_current": live_price,
                "profit": pnl,
                "is_be": False,
                "status": status,
                "score": sig.get("score"),
                "timestamp": sig.get("timestamp") or sig.get("created_at"),
            })

        # Leg 2: active if dual_entry is True and not a live ticket
        if dual and not (sig.get("exec_mode2") == "LIVE" and sig.get("ticket2")):
            entry2 = float(sig["fill_price2"]) if sig.get("fill_price2") else (
                float(sig["fill_price"]) if sig.get("fill_price") else float(sig["entry_price"])
            )
            lots2 = float(sig["lots2"]) if sig.get("lots2") else fixed_lot
            tp2 = float(sig["tp2_price"])
            is_be = (status == "HIT_TP1") or bool(sig.get("be_moved2"))
            sl2 = entry2 if is_be else float(sig["sl_price"])
            pnl2 = _calc_pnl(symbol, direction, lots2, entry2, live_price) if live_price is not None else 0.0
            positions.append({
                "signal_id": sig["id"],
                "leg": 2,
                "label": "TP2 (Runner)" if is_be else "TP2",
                "symbol": symbol,
                "direction": direction,
                "volume": lots2,
                "price_open": entry2,
                "sl": sl2,
                "tp": tp2,
                "price_current": live_price,
                "profit": pnl2,
                "is_be": is_be,
                "status": status,
                "score": sig.get("score"),
                "timestamp": sig.get("timestamp") or sig.get("created_at"),
            })

    return positions


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
            "• `/position` · `/close <ticket|all>`",
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
        if len(context.args) < 2 or context.args[1] != "CONFIRM":
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
    """Displays only open and running positions of dry run and live trades."""
    args = [a.lower() for a in (context.args or [])]

    filter_mode = "ALL"
    filter_map = {
        "bot": "BOT",
        "manual": "MANUAL",
        "man": "MANUAL",
        "dry": "DRY",
        "dryrun": "DRY",
        "sim": "DRY",
        "paper": "DRY",
        "live": "LIVE",
        "all": "ALL",
    }

    if args:
        sub = args[0]
        if sub in filter_map:
            filter_mode = filter_map[sub]
        else:
            await update.effective_message.reply_text(_POSITIONS_USAGE, parse_mode="Markdown")
            return

    curr, all_live, err = await asyncio.to_thread(_fetch_live_positions)
    magic = int(ALERT_STATE.get("magic_number", 770077))

    include_live = filter_mode in ("ALL", "LIVE", "BOT", "MANUAL")
    include_dry = filter_mode in ("ALL", "DRY", "BOT")

    mt5_warning = ""
    if err:
        mt5_warning = f"⚠️ *MT5 Offline:* `{err}` — Live positions unavailable.\n\n"
        live_positions = []
    elif filter_mode == "BOT":
        live_positions = [p for p in all_live if p.magic == magic]
    elif filter_mode == "MANUAL":
        live_positions = [p for p in all_live if p.magic != magic]
    elif filter_mode == "DRY":
        live_positions = []
    else:  # ALL or LIVE
        live_positions = all_live

    if not include_live:
        live_positions = []

    dry_positions = []
    if include_dry:
        dry_positions = await asyncio.to_thread(_fetch_dry_run_positions, curr)

    total_positions = len(live_positions) + len(dry_positions)

    if total_positions == 0:
        if filter_mode == "DRY":
            pos_text = "_No open dry-run positions currently._"
        elif filter_mode == "LIVE":
            pos_text = "_No open live positions currently._"
        elif filter_mode == "BOT":
            pos_text = "_No open bot positions currently (live or dry-run)._"
        elif filter_mode == "MANUAL":
            pos_text = "_No open manual positions currently._"
        else:
            pos_text = "_No open or running positions currently (dry-run or live)._"
    else:
        sections = []
        if include_live:
            if live_positions:
                live_lines = [f"🟢 **Live MT5 Positions ({len(live_positions)}):**"]
                for p in live_positions[:_MAX_POSITION_ROWS]:
                    side = "BUY 🟢" if p.type == mt5.POSITION_TYPE_BUY else "SELL 🔴"
                    src = "🤖" if p.magic == magic else "🖐️"
                    comment = f" `{md_safe(p.comment)}`" if p.comment else ""
                    live_lines.append(
                        f"• {src} `#{p.ticket}` {side} `{p.volume}` lots **{p.symbol}**{comment}\n"
                        f"   Entry `${p.price_open:.2f}` ➔ Now `${p.price_current:.2f}` | "
                        f"SL `${p.sl:.2f}` | TP `${p.tp:.2f}` | PnL: `{_money(p.profit, curr)}`"
                    )
                if len(live_positions) > _MAX_POSITION_ROWS:
                    live_lines.append(f"   … and `{len(live_positions) - _MAX_POSITION_ROWS}` more")
                total_live_float = sum(p.profit for p in live_positions)
                live_lines.append(f"   └ **Live Floating PnL:** `{_money(total_live_float, curr)}`")
                sections.append("\n".join(live_lines))
            elif include_dry and dry_positions:
                sections.append("🟢 **Live MT5 Positions:** _None active._")

        if include_dry:
            if dry_positions:
                dry_lines = [f"🧪 **Dry-Run Positions ({len(dry_positions)}):**"]
                for dp in dry_positions[:_MAX_POSITION_ROWS]:
                    side = "BUY 🟢" if dp["direction"] == "BUY" else "SELL 🔴"
                    be_note = " 🔒[BE]" if dp.get("is_be") else ""
                    now_str = f"${dp['price_current']:.2f}" if dp.get("price_current") is not None else "N/A"
                    pnl_str = _money(dp["profit"], curr) if dp.get("price_current") is not None else "N/A"
                    dry_lines.append(
                        f"• 🧪 `[DRY] #{dp['signal_id']} Leg {dp['leg']} ({dp['label']})` {side} `{dp['volume']}` lots **{dp['symbol']}**{be_note}\n"
                        f"   Entry `${dp['price_open']:.2f}` ➔ Now `{now_str}` | "
                        f"SL `${dp['sl']:.2f}` | TP `${dp['tp']:.2f}` | PnL: `{pnl_str}`"
                    )
                if len(dry_positions) > _MAX_POSITION_ROWS:
                    dry_lines.append(f"   … and `{len(dry_positions) - _MAX_POSITION_ROWS}` more")
                total_dry_float = sum(dp["profit"] for dp in dry_positions if dp.get("price_current") is not None)
                dry_lines.append(f"   └ **Dry-Run Floating PnL:** `{_money(total_dry_float, curr)}`")
                sections.append("\n".join(dry_lines))
            elif include_live and live_positions:
                sections.append("🧪 **Dry-Run Positions:** _None active._")

        # Summary total if both live and dry exist
        if live_positions and dry_positions:
            total_live_float = sum(p.profit for p in live_positions)
            total_dry_float = sum(dp["profit"] for dp in dry_positions if dp.get("price_current") is not None)
            sections.append(
                f"📊 **Combined Floating:** `{_money(total_live_float, curr)}` (Live) | "
                f"`{_money(total_dry_float, curr)}` (Dry-Run)"
            )

        pos_text = "\n\n".join(sections)

    filter_tag = f" `[{filter_mode}]`" if filter_mode != "ALL" else ""
    text = (
        f"💼 **OPEN & RUNNING POSITIONS ({total_positions})**{filter_tag}\n\n"
        f"{mt5_warning}"
        f"{pos_text}\n\n"
        f"💡 _Filter with `/position dry`, `/position live`, `/position bot`, `/position manual`._"
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
        await update.effective_message.reply_text("❌ Ticket and lots must be numeric.", parse_mode="Markdown")
        return

    if lots is not None and (not math.isfinite(lots) or lots <= 0):
        await update.effective_message.reply_text("❌ Lots must be a positive number.", parse_mode="Markdown")
        return

    res = await asyncio.to_thread(close_position, ticket, lots)
    icon = "✅" if res["ok"] else "❌"
    await update.effective_message.reply_text(
        f"{icon} **Close `#{ticket}`** — {md_safe(res['reason'])}", parse_mode="Markdown")


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

    # Guard: a BE stop only makes sense once price is beyond entry. Otherwise
    # trade_engine._adjust_levels would shove the SL next to market instead of failing.
    tick = await asyncio.to_thread(get_tick, pos.symbol)
    if tick is None:
        await update.effective_message.reply_text("❌ Unable to fetch a live tick.", parse_mode="Markdown")
        return
    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    ref = tick.bid if is_buy else tick.ask
    if (is_buy and ref <= pos.price_open) or ((not is_buy) and ref >= pos.price_open):
        await update.effective_message.reply_text(
            f"⚠️ **`#{ticket}` is not in profit** (price `${ref:.2f}` vs entry `${pos.price_open:.2f}`). "
            "A break-even stop would sit on the wrong side of the market — refusing.",
            parse_mode="Markdown")
        return

    res = await asyncio.to_thread(modify_position_sltp, ticket, pos.price_open, pos.tp or None)
    icon = "✅" if res["ok"] else "❌"
    await update.effective_message.reply_text(
        f"{icon} **`#{ticket}` SL → break-even** (`${pos.price_open:.2f}`) — {md_safe(res['reason'])}",
        parse_mode="Markdown")
