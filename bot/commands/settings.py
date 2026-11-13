import math
from telegram import Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, TIMEFRAME_PRESETS, save_settings
from strategies import STRATEGY_PRESETS, reset_runtime, resolve_param, params_overview, STRATEGIES, get_params, Params
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
    (("spread", "max_spread"), "max_allowed_spread_pips", _num(0, 500, lo_open=True)),

    (("risk", "risk_pct", "risk_percent"), "risk_percent", _num(0, 10, lo_open=True)),
    (("max_lot", "max_lot_size"), "max_lot_size", _num(0, 100, lo_open=True)),
    (("dyn_lots", "dynamic_lots"), "dynamic_lots_enabled", _bool),

    (("cb_enabled", "circuit_breaker", "cb_toggle"), "circuit_breaker_enabled", _bool),
    (("cb_losses", "max_losses", "cb_max_losses"), "max_daily_losses", _num(1, 100, integer=True)),
    (("cb_drawdown", "max_drawdown", "cb_max_drawdown"), "max_daily_drawdown_r", _num(0, 100, lo_open=True)),
    
    (("sl_cooldown", "cooldown", "sl_cooldown_enabled"), "sl_cooldown_enabled", _bool),
    (("cooldown_mins", "sl_cooldown_mins", "sl_cooldown_minutes"), "sl_cooldown_minutes", _num(0, 1440, integer=True)),

    (("magic", "magic_number"), "magic_number", _num(1, 2**31 - 1, integer=True)),
    (("slippage", "deviation", "max_slippage_points"), "max_slippage_points", _num(0, 1000, integer=True)),
    (("max_trades", "max_daily_trades"), "max_daily_trades", _num(1, 100, integer=True)),
    (("drift", "max_drift", "max_entry_drift_pct"), "max_entry_drift_pct", _num(0, 500, lo_open=True)),
    (("lot", "fixed_lot", "fixed_lot_size"), "fixed_lot_size", _num(0, 10, lo_open=True)),
    (("flatten_cb", "flatten_on_circuit_breaker"), "flatten_on_circuit_breaker", _bool),

    (("news_blockade", "news_toggle"), "news_blockade_enabled", _bool),
    (("news_before", "news_mins_before"), "news_blockade_mins_before", _num(0, 1440, integer=True)),
    (("news_after", "news_mins_after"), "news_blockade_mins_after", _num(0, 1440, integer=True)),
    (("news_impact", "news_level", "news_impacts", "news_blockade_impacts"), "news_blockade_impacts", _impacts),
]
_ALIAS_MAP = {alias: (name, parser) for aliases, name, parser in _SPECS for alias in aliases}


def _reset_dedup():
    reset_runtime()      # every strategy's dedup memory + the watchlist debounce


async def _set_strategy_param(update, key, val_str):
    """`/set <strategy>.<param> <value>` (or a bare alias when it's unambiguous / belongs to the active strategy)."""
    hits = resolve_param(key)
    if len(hits) > 1:
        active = [h for h in hits if h[0] == ALERT_STATE.get("active_strategy")]
        hits = active if len(active) == 1 else hits
    if not hits:
        await update.message.reply_text(f"❌ Unknown setting key `{key}`. Try `/set strategies`.", parse_mode="Markdown")
        return
    if len(hits) > 1:
        opts = ", ".join(f"`{s}.{prm.key}`" for s, prm in hits)
        await update.message.reply_text(f"⚠️ `{key}` matches several strategies — use one of: {opts}", parse_mode="Markdown")
        return
    skey, prm = hits[0]
    try:
        val = prm.parse(val_str)
    except ValueError as e:
        await update.message.reply_text(f"❌ Invalid value for `{key}`: {e}.", parse_mode="Markdown")
        return

    params = ALERT_STATE.setdefault("strategy_params", {}).setdefault(skey, {})
    if not STRATEGIES[skey].valid_params(Params({**params, prm.key: val})):
        await update.message.reply_text(
            f"❌ `{prm.key}={val}` conflicts with this strategy's other settings "
            "(e.g. buy RSI must stay below sell RSI).", parse_mode="Markdown")
        return
    old = params.get(prm.key)
    params[prm.key] = val
    save_settings()
    await update.message.reply_text(
        f"✅ **Strategy Setting Updated**\n\n`{skey}.{prm.key}`: `{old}` ➡️ `{val}`", parse_mode="Markdown")


@admin_only
async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dynamically updates bot settings (global ones in ALERT_STATE, strategy ones in strategy_params)."""
    if context.args and context.args[0].lower() in ("strategies", "params"):
        await update.message.reply_text(params_overview(), parse_mode="Markdown")
        return

    if not context.args or len(context.args) < 2:
        news_status = "ON 🟢" if ALERT_STATE.get("news_blockade_enabled", True) else "OFF 🔴"
        impacts_str = ", ".join(ALERT_STATE.get("news_blockade_impacts", ["high"]))
        reply = (
            "⚙️ **DYNAMIC SETTINGS MANAGER**\n\n"
            f"• **Max Spread (`spread`):** `{ALERT_STATE.get('max_allowed_spread_pips', 30)} pips`\n\n"

            "🤖 **Trade Execution (see `/trade` for full detail):**\n"
            f"• **Fixed Lot/Leg (`lot`):** `{ALERT_STATE.get('fixed_lot_size', 0.01)}`\n"
            f"• **Magic (`magic`)**, **Slippage (`slippage`)**, "
            f"**Max Trades/Day (`max_trades`)**, **Entry Drift (`drift`)**, **Flatten on CB (`flatten_cb`)**\n\n"

            "🚨 **Circuit Breaker Settings:**\n"
            f"• **Status (`cb_enabled`):** `{'ON 🟢' if ALERT_STATE.get('circuit_breaker_enabled', True) else 'OFF 🔴'}`\n"
            f"• **Max Consecutive Losses (`cb_losses`):** `{ALERT_STATE.get('max_daily_losses', 3)}`\n"
            f"• **Max Daily Drawdown (`cb_drawdown`):** `-{ALERT_STATE.get('max_daily_drawdown_r', 3.0)}R`\n\n"

            "⏸️ **SL Cooldown:**\n"
            f"• **Status (`sl_cooldown`):** `{'ON 🟢' if ALERT_STATE.get('sl_cooldown_enabled', True) else 'OFF 🔴'}`\n"
            f"• **Minutes (`cooldown_mins`):** `{ALERT_STATE.get('sl_cooldown_minutes', 30)}m`\n\n"

            "📰 **News Blockade Settings:**\n"
            f"• **Blockade Status (`news_blockade`):** `{news_status}`\n"
            f"• **Mins Before (`news_before`):** `{ALERT_STATE.get('news_blockade_mins_before', 30)}m`\n"
            f"• **Mins After (`news_after`):** `{ALERT_STATE.get('news_blockade_mins_after', 15)}m`\n"
            f"• **Impact Levels (`news_impact`):** `{impacts_str}`\n\n"

            "**Usage:** `/set <key> <value>` — e.g. `/set lot 0.02`, `/set news_impact high, medium`\n"
            "**Strategy settings:** `/set strategies` lists them · `/set <strategy>.<param> <value>`"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")

        active = ALERT_STATE.get("active_strategy")
        if active in STRATEGY_PRESETS:
            await update.message.reply_text(params_overview(active), parse_mode="Markdown")
        return

    key = context.args[0].lower().strip()
    val_str = " ".join(context.args[1:]).strip()

    spec = _ALIAS_MAP.get(key)
    if spec is None:
        await _set_strategy_param(update, key, val_str)
        return
    setting_name, parser = spec

    try:
        val = parser(val_str)
    except ValueError as e:
        await update.message.reply_text(f"❌ Invalid value for `{key}`: {e}.", parse_mode="Markdown")
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
    current = ALERT_STATE.get("active_strategy", "none")

    if not STRATEGY_PRESETS:
        await update.message.reply_text(
            "⚙️ **No strategies installed.**\n\n"
            "The scanner still runs analysis, but no signals are generated. "
            "Add a package to `strategies/` and restart the bot.",
            parse_mode="Markdown"
        )
        return

    choice = args[0].lower() if args else None
    if choice == "none":
        ALERT_STATE["active_strategy"] = "none"
        _reset_dedup()
        save_settings()
        await update.message.reply_text("⏹️ **Strategy disabled** — scanning continues, no signals will fire.", parse_mode="Markdown")
        return

    if choice not in STRATEGY_PRESETS:
        lines = "\n".join(f"• `{k}` — {v}" for k, v in STRATEGY_PRESETS.items())
        await update.message.reply_text(
            f"⚙️ **ACTIVE STRATEGY:** `{current.upper()}`\n\n"
            f"**Available:**\n{lines}\n\n"
            f"**Usage:** `/strategy <key>` · `/strategy none` · params: `/set strategies`",
            parse_mode="Markdown"
        )
        return

    ALERT_STATE["active_strategy"] = choice
    _reset_dedup()
    save_settings()
    await update.message.reply_text(
        f"✅ Active strategy updated to `{choice.upper()}`\n📝 `{STRATEGY_PRESETS[choice]}`",
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
            "• **Trigger distance:** set per strategy — `/set <strategy>.watch_margin <val>`\n"
            "**Usage:** `/watchlist on|off`",
            parse_mode="Markdown"
        )
        return
    sub = args[0].lower()
    if sub == "on":
        ALERT_STATE["setup_forming_enabled"] = True
    elif sub == "off":
        ALERT_STATE["setup_forming_enabled"] = False
    else:
        await update.message.reply_text("⚠️ Unknown option. Use `on` or `off`.", parse_mode="Markdown")
        return
    save_settings()
    await update.message.reply_text("✅ Watchlist setting updated.", parse_mode="Markdown")