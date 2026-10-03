import logging
import asyncio
from telegram import Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, TIMEFRAME_PRESETS, STRATEGY_PRESETS
from mt5_engine import get_gold_symbol
from strategy.backtester import run_backtest, generate_equity_chart
from ._common import admin_only


def _parse_rsi_pair(val_str):
    """Accepts '30/65' or '30,65'. Returns (buy, sell) floats or raises ValueError."""
    sep = "/" if "/" in val_str else ","
    parts = [p.strip() for p in val_str.split(sep) if p.strip()]
    if len(parts) != 2:
        raise ValueError("RSI override needs two numbers, e.g. rsi=30/65")
    return float(parts[0]), float(parts[1])


def _parse_backtest_overrides(tokens):
    """
    Parses key=value override tokens for a one-off /backtest run. These are NEVER
    written to ALERT_STATE/settings.json — they exist only for the duration of
    this single backtest call, so testing a hypothesis never requires a /set
    round-trip (and can't accidentally leave live scanning on a test value).

    Returns (overrides_dict, display_dict, error_str_or_None).
    """
    ALIASES = {
        "score": "min_confluence_score", "min_score": "min_confluence_score",
        "rrr": "min_rrr", "min_rrr": "min_rrr",
        "sl": "sl_atr_mult", "sl_mult": "sl_atr_mult",
        "tp1": "tp1_atr_mult", "tp1_mult": "tp1_atr_mult",
        "tp2": "tp2_atr_mult", "tp2_mult": "tp2_atr_mult",
        "strategy": "strategy_name", "strat": "strategy_name",
        "rsi": "rsi_pair",
    }
    overrides = {}
    display = {}

    for tok in tokens:
        if "=" not in tok:
            return None, None, f"`{tok}` isn't a `key=value` override."
        raw_key, raw_val = tok.split("=", 1)
        key = ALIASES.get(raw_key.strip().lower())
        if key is None:
            return None, None, (
                f"Unknown override key `{raw_key}`. Valid keys: "
                "`score`, `rrr`, `sl`, `tp1`, `tp2`, `strategy`, `rsi`."
            )
        val_str = raw_val.strip()
        try:
            if key == "min_confluence_score":
                overrides["min_confluence_score"] = int(float(val_str))
            elif key == "min_rrr":
                overrides["min_rrr"] = float(val_str)
            elif key == "sl_atr_mult":
                overrides["sl_atr_mult"] = float(val_str)
            elif key == "tp1_atr_mult":
                overrides["tp1_atr_mult"] = float(val_str)
            elif key == "tp2_atr_mult":
                overrides["tp2_atr_mult"] = float(val_str)
            elif key == "strategy_name":
                strat = val_str.lower()
                if strat not in STRATEGY_PRESETS:
                    return None, None, f"Unknown strategy `{val_str}`. Valid: `{'`, `'.join(STRATEGY_PRESETS)}`."
                overrides["strategy_name"] = strat
            elif key == "rsi_pair":
                buy, sell = _parse_rsi_pair(val_str)
                overrides["rsi_buy_threshold"], overrides["rsi_sell_threshold"] = buy, sell
        except ValueError:
            return None, None, f"Invalid value `{val_str}` for `{raw_key}`."
        display[raw_key.lower()] = val_str

    return overrides, display, None


@admin_only
async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    days = 30
    mode = None

    if args:
        remaining = list(args)
        try:
            days = int(remaining[0])
            remaining.pop(0)
        except ValueError:
            pass  # first token wasn't a day count — leave default, keep parsing

        override_tokens = []
        for tok in remaining:
            if "=" in tok:
                override_tokens.append(tok)
            elif tok.lower() in TIMEFRAME_PRESETS:
                mode = tok.lower()
            else:
                await update.message.reply_text(
                    "⚠️ **Usage:** `/backtest <days> [mode] [overrides...]`\n\n"
                    "**Overrides (never touch live settings — one-off for this run only):**\n"
                    "• `score=35` — min confluence score\n"
                    "• `rrr=2.0` — min reward:risk\n"
                    "• `sl=1.5` — SL ATR multiplier\n"
                    "• `tp1=2.0` / `tp2=3.5` — TP ATR multipliers\n"
                    "• `strategy=ema_cross` — override active strategy for this run\n"
                    "• `rsi=30/65` — RSI buy/sell thresholds\n\n"
                    "**Example:** `/backtest 30 scalp score=35 rrr=2.0 sl=1.5`",
                    parse_mode="Markdown"
                )
                return

        overrides, display, err = _parse_backtest_overrides(override_tokens)
        if err:
            await update.message.reply_text(f"❌ {err}", parse_mode="Markdown")
            return
    else:
        overrides, display = {}, {}

    days = max(1, min(days, 180))

    symbol = await asyncio.to_thread(get_gold_symbol)
    if not symbol:
        await update.message.reply_text("❌ MT5 Gold symbol not found.")
        return

    label = mode or ALERT_STATE['timeframe_mode']
    override_tag = f" `[{', '.join(f'{k}={v}' for k, v in display.items())}]`" if display else ""
    progress_msg = await update.message.reply_text(
        f"⏳ Running backtest — `{days}d` on `{label}`{override_tag}... `0%`", parse_mode="Markdown"
    )

    loop = asyncio.get_running_loop()

    def progress_callback(pct):
        async def _edit():
            try:
                await progress_msg.edit_text(
                    f"⏳ Running backtest — `{days}d` on `{label}`{override_tag}... `{pct}%`", parse_mode="Markdown"
                )
            except Exception:
                pass
        asyncio.run_coroutine_threadsafe(_edit(), loop)

    try:
        result = await asyncio.to_thread(
            run_backtest, symbol, days, mode, progress_callback=progress_callback, **overrides
        )
    except Exception as e:
        logging.exception("Backtest crashed")
        await update.message.reply_text(f"❌ **Backtest crashed:** `{e}`\n\nCheck the console log for the full traceback.", parse_mode="Markdown")
        return

    if "error" in result:
        await update.message.reply_text(f"❌ **Backtest failed:** {result['error']}", parse_mode="Markdown")
        return

    if result["total_trades"] == 0:
        await update.message.reply_text(
            f"📭 **No signals fired** over the last `{result['days']}d` on `{result['mode']}` "
            f"(min score `{result['min_confluence_score']}`, min RRR `1:{result['min_rrr']}`).",
            parse_mode="Markdown"
        )
        return

    factor_lines = "\n".join(
        f"  • {label}: `{wr}%` win rate ({n} occurrences)"
        for label, wr, n in result["factor_summary"][:6]
    ) or "  • Not enough closed trades yet for a factor breakdown."

    # Effective parameters actually used: overrides win, otherwise fall back to
    # live ALERT_STATE — same resolution order run_backtest() itself applies.
    strat = (overrides.get("strategy_name") or ALERT_STATE.get("active_strategy", "smc_confluence")).replace("_", " ").upper()
    rsi_buy = overrides.get("rsi_buy_threshold", ALERT_STATE.get("rsi_buy_threshold", 30))
    rsi_sell = overrides.get("rsi_sell_threshold", ALERT_STATE.get("rsi_sell_threshold", 70))
    sl_mult = overrides.get("sl_atr_mult", ALERT_STATE.get("sl_atr_mult", 1.5))
    tp1_mult = overrides.get("tp1_atr_mult", ALERT_STATE.get("tp1_atr_mult", 1.0))
    tp2_mult = overrides.get("tp2_atr_mult", ALERT_STATE.get("tp2_atr_mult", 2.0))
    struct_gate = "ON ✅" if ALERT_STATE.get("require_structure_break") else "OFF ❌"
    vol_gate = "ON ✅" if ALERT_STATE.get("require_volume_atr_filter") else "OFF ❌"

    override_note = (
        f"\n🧪 **One-off overrides (live settings untouched):** `{', '.join(f'{k}={v}' for k, v in display.items())}`\n"
        if display else ""
    )

    msg = (
        f"🧪 **BACKTEST RESULTS — {result['mode'].upper()}** ({result['days']}d)\n"
        f"⚙️ **Strategy:** `{strat}`{override_note}\n"
        f"**--- Parameters Used ---**\n"
        f"• **RSI Thresholds:** Buy `<= {rsi_buy}` | Sell `>= {rsi_sell}`\n"
        f"• **ATR Multipliers:** SL `{sl_mult}x` | TP1 `{tp1_mult}x` | TP2 `{tp2_mult}x`\n"
        f"• **Score & RRR:** Min Score `{result['min_confluence_score']}/100` | Min RRR `1:{result['min_rrr']}`\n"
        f"• **Hard Gates:** Structure: {struct_gate} | Vol/ATR: {vol_gate}\n\n"
        f"**--- Performance ---**\n"
        f"• **Trades:** `{result['total_trades']}` | **Wins:** `{result['wins']}` | **Losses:** `{result['losses']}` | **Open:** `{result['open']}`\n"
        f"• **Win Rate:** `{result['win_rate']}%`\n"
        f"• **Avg R / Trade:** `{result['avg_r']}R` | **Net R:** `{result['net_r']}R`\n"
        f"• **Max Drawdown:** `{result['max_drawdown_r']}R`\n\n"
        f"📊 **Top Confluence Factors:**\n{factor_lines}\n\n"
        f"⚠️ *Simulated on historical bars with a synthetic spread and single fixed-size position — "
        f"the live bot's dual-entry sizing isn't modeled here. Real fills, slippage, and news gaps will vary.*"
    )

    chart_buf = generate_equity_chart(result["equity_curve"], title=f"XAUUSD Backtest Equity — {result['mode'].upper()} ({result['days']}d)")
    if chart_buf:
        if len(msg) <= 1024:
            await update.message.reply_photo(photo=chart_buf, caption=msg, parse_mode="Markdown")
        else:
            await update.message.reply_text(msg, parse_mode="Markdown")
            await update.message.reply_photo(
                photo=chart_buf,
                caption=f"📈 **XAUUSD Backtest Equity Curve** (`{result['mode'].upper()}` | `{result['days']}d`)",
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")