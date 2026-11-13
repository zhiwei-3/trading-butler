import logging
import asyncio
from telegram import Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, TIMEFRAME_PRESETS
from strategies import STRATEGIES, STRATEGY_PRESETS, find_param, get_params, Params
from core.mt5_engine import get_gold_symbol
from core.backtester import run_backtest, generate_equity_chart
from ._common import admin_only

_STRAT_KEYS = ("strategy", "strat")


def _parse_backtest_overrides(tokens):
    """
    key=value tokens for a ONE-OFF /backtest run (never written to settings). `strategy=<key>` picks the
    strategy; every other key is one of THAT strategy's params (key, alias or 'strategy.key').
    Returns (strategy_key, param_overrides, display, error).
    """
    pairs = []
    for tok in tokens:
        if "=" not in tok:
            return None, None, None, f"`{tok}` isn't a `key=value` override."
        k, v = tok.split("=", 1)
        pairs.append((k.strip().lower(), v.strip()))

    strat_key = ALERT_STATE.get("active_strategy")
    for k, v in pairs:
        if k in _STRAT_KEYS:
            if v.lower() not in STRATEGY_PRESETS:
                return None, None, None, f"Unknown strategy `{v}`. Valid: `{'`, `'.join(STRATEGY_PRESETS) or 'none installed'}`."
            strat_key = v.lower()

    overrides, display = {}, {}
    for k, v in pairs:
        if k in _STRAT_KEYS:
            display[k] = v
            continue
        prm = find_param(strat_key, k)
        if prm is None:
            valid = [p.key for p in STRATEGIES[strat_key].params] if strat_key in STRATEGIES else []
            return None, None, None, (f"`{k}` isn't a parameter of `{strat_key}`. "
                                      f"Valid: `strategy`, `{'`, `'.join(valid)}`.")
        try:
            overrides[prm.key] = prm.parse(v)
        except ValueError as e:
            return None, None, None, f"Invalid value `{v}` for `{k}` ({e})."
        display[k] = v

    if strat_key in STRATEGIES and overrides:
        merged = Params({**get_params(strat_key), **overrides})
        if not STRATEGIES[strat_key].valid_params(merged):
            return None, None, None, "Those overrides are inconsistent with each other (e.g. buy RSI must be below sell RSI)."
    return strat_key, overrides, display, None


@admin_only
async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    days = 30
    mode = None
    strat_key, overrides, display = None, {}, {}

    if args:
        remaining = list(args)
        try:
            days = int(remaining[0])
            remaining.pop(0)
        except ValueError:
            pass

        override_tokens = []
        for tok in remaining:
            if "=" in tok:
                override_tokens.append(tok)
            elif tok.lower() in TIMEFRAME_PRESETS:
                mode = tok.lower()
            else:
                await update.message.reply_text(
                    "⚠️ **Usage:** `/backtest <days> [mode] [overrides...]`\n\n"
                    "**Overrides (one-off, never touch live settings):**\n"
                    "• `strategy=<key>` — run a different strategy\n"
                    "• `<param>=<value>` — any parameter of that strategy (see `/set strategies`)\n\n"
                    "**Example:** `/backtest 30 scalp rrr=2.0 sl=1.5`",
                    parse_mode="Markdown"
                )
                return

        strat_key, overrides, display, err = _parse_backtest_overrides(override_tokens)
        if err:
            await update.message.reply_text(f"❌ {err}", parse_mode="Markdown")
            return

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
            run_backtest, symbol, days, mode, progress_callback=progress_callback,
            strategy_name=strat_key, param_overrides=overrides
        )
    except Exception as e:
        logging.exception("Backtest crashed")
        await update.message.reply_text(f"❌ **Backtest crashed:** `{e}`\n\nCheck the console log for the full traceback.", parse_mode="Markdown")
        return

    if "error" in result:
        await update.message.reply_text(f"❌ **Backtest failed:** {result['error']}", parse_mode="Markdown")
        return

    strat = STRATEGIES[result["strategy"]]
    summary = strat.summary(Params(result["params"]))

    if result["total_trades"] == 0:
        await update.message.reply_text(
            f"📭 **No signals fired** over the last `{result['days']}d` on `{result['mode']}` "
            f"for `{result['strategy']}` (`{summary}`).",
            parse_mode="Markdown"
        )
        return

    factor_lines = "\n".join(
        f"  • {name}: `{wr}%` win rate ({n} occurrences)"
        for name, wr, n in result["factor_summary"][:6]
    ) or "  • Not enough closed trades yet for a factor breakdown."

    param_lines = "\n".join(f"  • `{k}` = `{v}`" for k, v in list(result["params"].items())[:12])
    struct_gate = "ON ✅" if ALERT_STATE.get("require_structure_break") else "OFF ❌"
    vol_gate = "ON ✅" if ALERT_STATE.get("require_volume_atr_filter") else "OFF ❌"
    override_note = (
        f"\n🧪 **One-off overrides (live settings untouched):** `{', '.join(f'{k}={v}' for k, v in display.items())}`\n"
        if display else ""
    )

    msg = (
        f"🧪 **BACKTEST RESULTS — {result['mode'].upper()}** ({result['days']}d)\n"
        f"⚙️ **Strategy:** `{result['strategy'].replace('_', ' ').upper()}`{override_note}\n"
        f"**--- Parameters Used ---**\n{param_lines}\n"
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