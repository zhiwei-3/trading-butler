from telegram import Update
from telegram.ext import ContextTypes

from config import (
    ALERT_STATE, TIMEFRAME_PRESETS, STRATEGY_PRESETS,
    CONFLUENCE_WEIGHTS, save_settings,
)
from ._common import admin_only


@admin_only
async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dynamically updates bot settings in ALERT_STATE."""
    if not context.args or len(context.args) < 2:
        news_status = "ON 🟢" if ALERT_STATE.get("news_blockade_enabled", True) else "OFF 🔴"
        impacts_str = ", ".join(ALERT_STATE.get("news_blockade_impacts", ["high"]))

        reply = (
            "⚙️ **DYNAMIC SETTINGS MANAGER**\n\n"
            f"• **Buy RSI (`buy_rsi`):** `{ALERT_STATE.get('rsi_buy_threshold', 30)}`\n"
            f"• **Sell RSI (`sell_rsi`):** `{ALERT_STATE.get('rsi_sell_threshold', 70)}`\n"
            f"• **Min Score (`score`):** `{ALERT_STATE.get('min_confluence_score', 50)}/100`\n"
            f"• **Min RRR (`rrr`):** `1:{ALERT_STATE.get('min_rrr', 1.3)}`\n"
            f"• **SL ATR Mult (`sl_mult`):** `{ALERT_STATE.get('sl_atr_mult', 1.5)}x`\n"
            f"• **TP1 ATR Mult (`tp1_mult`):** `{ALERT_STATE.get('tp1_atr_mult', 1.0)}x`\n"
            f"• **TP2 ATR Mult (`tp2_mult`):** `{ALERT_STATE.get('tp2_atr_mult', 2.0)}x`\n"
            f"• **Max Spread (`spread`):** `{ALERT_STATE.get('max_allowed_spread_pips', 30)} pips`\n\n"

            "🤖 **Trade Execution (see `/trade` for full detail):**\n"
            f"• **Fixed Lot/Leg (`lot`):** `{ALERT_STATE.get('fixed_lot_size', 0.01)}`\n"
            f"• **Dual-Entry Score (`dual_score`):** `{ALERT_STATE.get('dual_entry_score_threshold', 50)}`\n"
            f"• **Manual Calc Risk % (`risk`):** `{ALERT_STATE.get('risk_percent', 1.0)}%` *(reference only, see `/calc`)*\n"
            f"• **Magic (`magic`)**, **Slippage (`slippage`)**, **Max Positions (`max_positions`)**, "
            f"**Max Trades/Day (`max_trades`)**, **Entry Drift (`drift`)**, **Flatten on CB (`flatten_cb`)**\n\n"

            "🚨 **Circuit Breaker Settings:**\n"
            f"• **Status (`cb_enabled`):** `{'ON 🟢' if ALERT_STATE.get('circuit_breaker_enabled', True) else 'OFF 🔴'}`\n"
            f"• **Max Consecutive Losses (`cb_losses`):** `{ALERT_STATE.get('max_daily_losses', 3)}`\n"
            f"• **Max Daily Drawdown (`cb_drawdown`):** `-{ALERT_STATE.get('max_daily_drawdown_r', 3.0)}R`\n\n"

            "📰 **News Blockade Settings:**\n"
            f"• **Blockade Status (`news_blockade`):** `{news_status}`\n"
            f"• **Mins Before (`news_before`):** `{ALERT_STATE.get('news_blockade_mins_before', 30)}m`\n"
            f"• **Mins After (`news_after`):** `{ALERT_STATE.get('news_blockade_mins_after', 15)}m`\n"
            f"• **Impact Levels (`news_impact`):** `{impacts_str}`\n\n"

            "**Usage:** `/set <key> <value>`\n"
            "• `/set tp1_mult 1.5`\n"
            "• `/set lot 0.02`\n"
            "• `/set dual_score 60`\n"
            "• `/set news_impact high, medium`"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")
        return

    key = context.args[0].lower().strip()
    # Join all remaining arguments so comma-separated strings with spaces parse cleanly
    val_str = " ".join(context.args[1:]).strip()

    try:
        setting_name = ""
        val = None

        # Map shorthand keys to actual dictionary keys
        if key in ("buy_rsi", "rsi_buy", "rsi_buy_threshold"):
            setting_name, val = "rsi_buy_threshold", float(val_str)
        elif key in ("sell_rsi", "rsi_sell", "rsi_sell_threshold"):
            setting_name, val = "rsi_sell_threshold", float(val_str)
        elif key in ("score", "min_score", "min_confluence_score"):
            setting_name, val = "min_confluence_score", int(float(val_str))
        elif key in ("rrr", "min_rrr"):
            setting_name, val = "min_rrr", float(val_str)
        elif key in ("risk", "risk_percent", "risk_pct"):
            setting_name, val = "risk_percent", float(val_str)
        elif key in ("sl_mult", "sl_atr"):
            setting_name, val = "sl_atr_mult", float(val_str)
        elif key in ("tp1_mult", "tp1_atr"):
            setting_name, val = "tp1_atr_mult", float(val_str)
        elif key in ("tp2_mult", "tp2_atr"):
            setting_name, val = "tp2_atr_mult", float(val_str)
        elif key in ("spread", "max_spread"):
            setting_name, val = "max_allowed_spread_pips", float(val_str)

        elif key in ("cb_enabled", "circuit_breaker", "cb_toggle"):
            setting_name, val = "circuit_breaker_enabled", val_str.lower() in ("true", "1", "on", "yes")
        elif key in ("cb_losses", "max_losses", "cb_max_losses"):
            setting_name, val = "max_daily_losses", int(val_str)
        elif key in ("cb_drawdown", "max_drawdown", "cb_max_drawdown"):
            setting_name, val = "max_daily_drawdown_r", float(val_str)

        elif key in ("magic", "magic_number"):
            setting_name, val = "magic_number", int(val_str)
        elif key in ("slippage", "deviation", "max_slippage_points"):
            setting_name, val = "max_slippage_points", int(val_str)
        elif key in ("max_positions", "max_open_positions"):
            setting_name, val = "max_open_positions", int(val_str)
        elif key in ("max_trades", "max_daily_trades"):
            setting_name, val = "max_daily_trades", int(val_str)
        elif key in ("drift", "max_drift", "max_entry_drift_pct"):
            setting_name, val = "max_entry_drift_pct", float(val_str)
        elif key in ("lot", "fixed_lot", "fixed_lot_size"):
            setting_name, val = "fixed_lot_size", float(val_str)
        elif key in ("dual_score", "dual_entry_score", "dual_threshold", "dual_entry_score_threshold"):
            setting_name, val = "dual_entry_score_threshold", int(val_str)
        elif key in ("flatten_cb", "flatten_on_circuit_breaker"):
            setting_name, val = "flatten_on_circuit_breaker", val_str.lower() in ("true", "1", "on", "yes")

        elif key in ("news_blockade", "news_toggle"):
            setting_name, val = "news_blockade_enabled", val_str.lower() in ("true", "1", "on", "yes")
        elif key in ("news_before", "news_mins_before"):
            setting_name, val = "news_blockade_mins_before", int(val_str)
        elif key in ("news_after", "news_mins_after"):
            setting_name, val = "news_blockade_mins_after", int(val_str)
        elif key in ("news_impact", "news_level", "news_impacts", "news_blockade_impacts"):
            setting_name = "news_blockade_impacts"
            val = [x.strip().lower() for x in val_str.split(",") if x.strip()]
        else:
            await update.message.reply_text(f"❌ Unknown setting key `{key}`.", parse_mode="Markdown")
            return

        old_val = ALERT_STATE.get(setting_name, "N/A")
        ALERT_STATE[setting_name] = val
        save_settings()

        display_name = setting_name.replace("_", " ").title()
        formatted_val = ", ".join([v.upper() for v in val]) if isinstance(val, list) else val
        formatted_old = ", ".join([v.upper() for v in old_val]) if isinstance(old_val, list) else old_val

        await update.message.reply_text(
            f"✅ **Setting Updated**\n\n"
            f"**{display_name}** modified:\n"
            f"`{formatted_old}` ➡️ `{formatted_val}`",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid value provided for this setting.", parse_mode="Markdown")


@admin_only
async def set_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or args[0].lower() not in TIMEFRAME_PRESETS:
        preset_lines = "\n".join(f"• `{k}` → {v['label']}" for k, v in TIMEFRAME_PRESETS.items())
        await update.message.reply_text(f"⚠️ **Usage:** `/timeframe <mode>`\n\n{preset_lines}", parse_mode="Markdown")
        return
    mode = args[0].lower()
    preset = TIMEFRAME_PRESETS[mode]
    ALERT_STATE["timeframe_mode"] = mode
    ALERT_STATE["entry_tf"] = preset["entry"]
    ALERT_STATE["trend_tf"] = preset["trend"]
    ALERT_STATE["macro_tf"] = preset["macro"]
    ALERT_STATE["last_rsi_signal"] = None
    save_settings()
    await update.message.reply_text(f"✅ Timeframe set to `{mode.upper()}` ({preset['label']})", parse_mode="Markdown")


@admin_only
async def set_strategy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    current = ALERT_STATE.get("active_strategy", "smc_confluence")

    if not args or args[0].lower() not in STRATEGY_PRESETS:
        lines = "\n".join(f"• `{k}` — {v}" for k, v in STRATEGY_PRESETS.items())
        await update.message.reply_text(
            f"⚙️ **ACTIVE STRATEGY:** `{current.upper()}`\n\n"
            f"**Available Presets:**\n{lines}\n\n"
            f"**Usage:** `/strategy smc_confluence` or `/strategy ema_cross`\n\n"
            f"ℹ️ *Dual-entry (2 legs) only applies to `smc_confluence`. Every other strategy always "
            f"trades a single TP1-only leg.*",
            parse_mode="Markdown"
        )
        return

    mode = args[0].lower()
    ALERT_STATE["active_strategy"] = mode
    # Switching strategy must clear the previous strategy's dedup state, or it
    # could silently suppress the first signal on the newly selected strategy.
    ALERT_STATE["last_rsi_signal"] = None
    save_settings()

    strategy_label = STRATEGY_PRESETS[mode]
    await update.message.reply_text(
        f"✅ Active strategy updated to `{mode.upper()}`\n"
        f"📝 `{strategy_label}`",
        parse_mode="Markdown"
    )


@admin_only
async def filters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        reply = (
            "🎛️ **STRATEGY FILTERS**\n\n"
            f"• **Structure (BOS):** {'ON ✅' if ALERT_STATE['require_structure_break'] else 'OFF ❌'}\n"
            f"• **Volume/ATR Filter:** {'ON ✅' if ALERT_STATE['require_volume_atr_filter'] else 'OFF ❌'}\n\n"
            "**Usage:** `/filters structure on|off` or `/filters volume on|off`"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")
        return

    if len(args) >= 2:
        key, value = args[0].lower(), args[1].lower()
        if key == "structure" and value in ("on", "off"):
            ALERT_STATE["require_structure_break"] = (value == "on")
            save_settings()
            await update.message.reply_text(f"✅ Structure filter **{value.upper()}**", parse_mode="Markdown")
            return
        elif key == "volume" and value in ("on", "off"):
            ALERT_STATE["require_volume_atr_filter"] = (value == "on")
            save_settings()
            await update.message.reply_text(f"✅ Volume/ATR filter **{value.upper()}**", parse_mode="Markdown")
            return
    await update.message.reply_text("⚠️ Invalid format. Send `/filters` alone to see usage.", parse_mode="Markdown")


@admin_only
async def confluence_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        weight_lines = "\n".join(f"  • {k.replace('_', ' ').title()}: `{v} pts`" for k, v in CONFLUENCE_WEIGHTS.items())
        await update.message.reply_text(
            f"🎯 **CONFLUENCE SCORING**\n\nMinimum score to fire: `{ALERT_STATE['min_confluence_score']}/100`\n"
            f"Dual-entry (2 legs) threshold: `{ALERT_STATE.get('dual_entry_score_threshold', 50)}/100`\n\n"
            f"{weight_lines}\n\n**Usage:** `/confluence <0-100>`", parse_mode="Markdown")
        return
    try:
        val = int(args[0])
        if 0 <= val <= 100:
            ALERT_STATE["min_confluence_score"] = val
            save_settings()
            await update.message.reply_text(f"✅ Minimum confluence score set to `{val}/100`", parse_mode="Markdown")
        else:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Provide a number between 0 and 100.")


@admin_only
async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "👀 **WATCHLIST SETTINGS**\n\n"
            f"• **Status:** {'ON ✅' if ALERT_STATE['setup_forming_enabled'] else 'OFF ❌'}\n"
            f"• **RSI Margin:** `{ALERT_STATE['watch_rsi_margin']}` pts\n"
            f"• **Score Margin:** `{ALERT_STATE['watch_score_margin']}` pts\n\n"
            "**Usage:** `/watchlist on|off` or `/watchlist rsi_margin <val>`",
            parse_mode="Markdown"
        )
        return
    sub = args[0].lower()
    if sub == "on":
        ALERT_STATE["setup_forming_enabled"] = True
    elif sub == "off":
        ALERT_STATE["setup_forming_enabled"] = False
    elif sub == "rsi_margin" and len(args) >= 2:
        try:
            ALERT_STATE["watch_rsi_margin"] = float(args[1])
        except ValueError:
            await update.message.reply_text("❌ Provide a numeric RSI margin.", parse_mode="Markdown")
            return
    else:
        await update.message.reply_text("⚠️ Unknown option. Use `on`, `off`, or `rsi_margin <val>`.", parse_mode="Markdown")
        return
    save_settings()
    await update.message.reply_text("✅ Watchlist setting updated.", parse_mode="Markdown")