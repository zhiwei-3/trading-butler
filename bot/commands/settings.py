import math
from telegram import Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, TIMEFRAME_PRESETS, STRATEGY_PRESETS, save_settings
from ._common import admin_only


# ---------------------------------------------------------------- parsers

def _num(lo=None, hi=None, integer=False, lo_open=False):
    """Builds a strict numeric parser: rejects NaN/inf, non-integers (if integer), out-of-range."""
    def parse(s):
        try:
            v = float(s)
        except ValueError:
            raise ValueError("not a number")
        if not math.isfinite(v):
            raise ValueError("not a finite number")
        if integer:
            if not v.is_integer():
                raise ValueError("must be a whole number")
            v = int(v)
        if lo is not None and (v < lo or (lo_open and v == lo)):
            raise ValueError(f"must be {'>' if lo_open else '>='} {lo}")
        if hi is not None and v > hi:
            raise ValueError(f"must be <= {hi}")
        return v
    return parse


def _bool(s):
    s = s.strip().lower()
    if s in ("true", "1", "on", "yes"):
        return True
    if s in ("false", "0", "off", "no"):
        return False
    raise ValueError("use on/off")


def _impacts(s):
    val = [x.strip().lower() for x in s.replace(",", " ").split() if x.strip()]
    bad = [v for v in val if v not in ("high", "medium", "low", "holiday")]
    if not val or bad:
        raise ValueError("impact levels must be from: high, medium, low, holiday")
    return val


# (aliases, ALERT_STATE key, parser)
_SPECS = [
    (("buy_rsi", "rsi_buy", "rsi_buy_threshold"), "rsi_buy_threshold", _num(1, 99)),
    (("sell_rsi", "rsi_sell", "rsi_sell_threshold"), "rsi_sell_threshold", _num(1, 99)),
    (("score", "min_score", "min_confluence_score"), "min_confluence_score", _num(0, 100, integer=True)),
    (("rrr", "min_rrr"), "min_rrr", _num(0, 20, lo_open=True)),
    (("sl_mult", "sl_atr"), "sl_atr_mult", _num(0, 20, lo_open=True)),
    (("tp1_mult", "tp1_atr"), "tp1_atr_mult", _num(0, 20, lo_open=True)),
    (("tp2_mult", "tp2_atr"), "tp2_atr_mult", _num(0, 20, lo_open=True)),
    (("spread", "max_spread"), "max_allowed_spread_pips", _num(0, 500, lo_open=True)),

    (("ifvg_lookback", "ifvg_look"), "ifvg_lookback", _num(30, 200, integer=True)),
    (("ifvg_tol", "ifvg_eq_tol"), "ifvg_eq_tolerance_atr", _num(0, 5, lo_open=True)),
    (("ifvg_bars", "ifvg_max_bars"), "ifvg_max_setup_bars", _num(1, 200, integer=True)),
    (("ifvg_body", "ifvg_min_body"), "ifvg_min_body_atr", _num(0, 5)),
    (("ifvg_buffer", "ifvg_sl_buffer"), "ifvg_sl_buffer_atr", _num(0, 5)),
    (("ifvg_max_sl",), "ifvg_max_sl_atr", _num(0, 10, lo_open=True)),
    (("ifvg_rr", "ifvg_fallback_rr"), "ifvg_fallback_rr", _num(0, 20, lo_open=True)),

    (("cb_enabled", "circuit_breaker", "cb_toggle"), "circuit_breaker_enabled", _bool),
    (("cb_losses", "max_losses", "cb_max_losses"), "max_daily_losses", _num(1, 100, integer=True)),
    (("cb_drawdown", "max_drawdown", "cb_max_drawdown"), "max_daily_drawdown_r", _num(0, 100, lo_open=True)),

    (("magic", "magic_number"), "magic_number", _num(1, 2**31 - 1, integer=True)),
    (("slippage", "deviation", "max_slippage_points"), "max_slippage_points", _num(0, 1000, integer=True)),
    (("max_trades", "max_daily_trades"), "max_daily_trades", _num(1, 100, integer=True)),
    (("drift", "max_drift", "max_entry_drift_pct"), "max_entry_drift_pct", _num(0, 500, lo_open=True)),
    (("lot", "fixed_lot", "fixed_lot_size"), "fixed_lot_size", _num(0, 10, lo_open=True)),
    (("dual_score", "dual_entry_score", "dual_threshold", "dual_entry_score_threshold"),
     "dual_entry_score_threshold", _num(0, 100, integer=True)),
    (("flatten_cb", "flatten_on_circuit_breaker"), "flatten_on_circuit_breaker", _bool),

    (("news_blockade", "news_toggle"), "news_blockade_enabled", _bool),
    (("news_before", "news_mins_before"), "news_blockade_mins_before", _num(0, 1440, integer=True)),
    (("news_after", "news_mins_after"), "news_blockade_mins_after", _num(0, 1440, integer=True)),
    (("news_impact", "news_level", "news_impacts", "news_blockade_impacts"), "news_blockade_impacts", _impacts),
]
_ALIAS_MAP = {alias: (name, parser) for aliases, name, parser in _SPECS for alias in aliases}


def _reset_dedup():
    ALERT_STATE["last_rsi_signal"] = None
    ALERT_STATE["last_ifvg_signal"] = None
    ALERT_STATE["last_watch_signal"] = None


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
            f"• **Magic (`magic`)**, **Slippage (`slippage`)**, "
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
    val_str = " ".join(context.args[1:]).strip()

    spec = _ALIAS_MAP.get(key)
    if spec is None:
        await update.message.reply_text(f"❌ Unknown setting key `{key}`.", parse_mode="Markdown")
        return
    setting_name, parser = spec

    try:
        val = parser(val_str)
    except ValueError as e:
        await update.message.reply_text(f"❌ Invalid value for `{key}`: {e}.", parse_mode="Markdown")
        return

    # Cross-field sanity: buy threshold must stay below sell threshold.
    if setting_name == "rsi_buy_threshold" and val >= ALERT_STATE.get("rsi_sell_threshold", 70):
        await update.message.reply_text("❌ Buy RSI must be lower than Sell RSI.", parse_mode="Markdown")
        return
    if setting_name == "rsi_sell_threshold" and val <= ALERT_STATE.get("rsi_buy_threshold", 30):
        await update.message.reply_text("❌ Sell RSI must be higher than Buy RSI.", parse_mode="Markdown")
        return

    old_val = ALERT_STATE.get(setting_name, "N/A")
    ALERT_STATE[setting_name] = val
    save_settings()

    display_name = setting_name.replace("_", " ").title()
    formatted_val = ", ".join(v.upper() for v in val) if isinstance(val, list) else val
    formatted_old = ", ".join(v.upper() for v in old_val) if isinstance(old_val, list) else old_val

    await update.message.reply_text(
        f"✅ **Setting Updated**\n\n"
        f"**{display_name}** modified:\n"
        f"`{formatted_old}` ➡️ `{formatted_val}`",
        parse_mode="Markdown"
    )


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
    _reset_dedup()
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
    _reset_dedup()   # a stale dedup key could silently suppress the first signal of the new strategy
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
async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "👀 **WATCHLIST SETTINGS**\n\n"
            f"• **Status:** {'ON ✅' if ALERT_STATE['setup_forming_enabled'] else 'OFF ❌'}\n"
            f"• **RSI Margin:** `{ALERT_STATE['watch_rsi_margin']}` pts\n"
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
            ALERT_STATE["watch_rsi_margin"] = _num(0, 50)(args[1])
        except ValueError as e:
            await update.message.reply_text(f"❌ Invalid RSI margin: {e}.", parse_mode="Markdown")
            return
    else:
        await update.message.reply_text("⚠️ Unknown option. Use `on`, `off`, or `rsi_margin <val>`.", parse_mode="Markdown")
        return
    save_settings()
    await update.message.reply_text("✅ Watchlist setting updated.", parse_mode="Markdown")