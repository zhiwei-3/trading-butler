from telegram import Update
from telegram.ext import ContextTypes

from config import (ALERT_STATE, TIMEFRAME_PRESETS, save_settings, apply_timeframe_mode,
                    find_setting, settable_groups)
from strategies import (STRATEGY_PRESETS, STRATEGIES, reset_runtime, resolve_param,
                        params_overview, Params, get_params, gates_summary)
from bot.registry import command
from utils.schedule import parse_schedule


_GATES = {"structure": "require_structure", "volume": "require_volume"}


# ---------------------------------------------------------------- core settings (generated from config.SCHEMA)

def _fmt(spec, val):
    if isinstance(val, bool):
        return "ON 🟢" if val else "OFF 🔴"
    if isinstance(val, (list, tuple)):
        return ", ".join(str(v).upper() for v in val)
    return f"{val}{spec.unit}"


def _overview():
    lines = ["⚙️ **DYNAMIC SETTINGS MANAGER**\n"]
    for group, specs in settable_groups().items():
        lines.append(f"**{group}**")
        for s in specs:
            lines.append(f"• {s.label} (`{s.aliases[0]}`): `{_fmt(s, ALERT_STATE.get(s.key))}`")
        lines.append("")
    lines.append("**Usage:** `/set <key> <value>` — e.g. `/set lot 0.02`, `/set news_impact high, medium`\n"
                 "**Strategy settings:** `/set strategies` lists them · `/set <strategy>.<param> <value>`\n"
                 "**Batch:** put several commands on separate lines of one message "
                 "(`/set`, `/filters`, `/watchlist`, `/timeframe`, `/strategy`, `/heartbeat`, scanner on/off).")
    return "\n".join(lines)


# ---------------------------------------------------------------- strategy params

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


# ---------------------------------------------------------------- /set

@command("set", "View/Adjust Dynamic Parameters", icon="🔧", group="Strategy & Parameters", usage="/set <key> <val>", order=3)
async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Updates bot settings: core ones (config.SCHEMA) or strategy ones (strategy_params)."""
    args = context.args or []

    if args and args[0].lower() in ("strategies", "params"):
        await update.message.reply_text(params_overview(), parse_mode="Markdown")
        return

    if len(args) == 1:
        spec = find_setting(args[0])
        if spec:
            await update.message.reply_text(
                f"⚙️ **{spec.label}** (`{spec.aliases[0]}`): `{_fmt(spec, ALERT_STATE.get(spec.key))}`\n"
                f"Change it: `/set {spec.aliases[0]} <value>`", parse_mode="Markdown")
            return

    if len(args) < 2:
        await update.message.reply_text(_overview(), parse_mode="Markdown")
        active = ALERT_STATE.get("active_strategy")
        if active in STRATEGY_PRESETS:
            await update.message.reply_text(params_overview(active), parse_mode="Markdown")
        return

    key = args[0].lower().strip()
    val_str = " ".join(args[1:]).strip()

    spec = find_setting(key)
    if spec is None:
        await _set_strategy_param(update, key, val_str)
        return

    try:
        val = spec.parse(val_str)
    except ValueError as e:
        await update.message.reply_text(f"❌ Invalid value for `{key}`: {e}.", parse_mode="Markdown")
        return

    if spec.key == "strategy_schedule":
        unknown = [k for k, _, _ in parse_schedule(val) if k not in STRATEGIES]
        if unknown:
            await update.message.reply_text(
                f"❌ Unknown strategy `{', '.join(unknown)}`. Installed: `{'`, `'.join(STRATEGIES) or 'none'}`.",
                parse_mode="Markdown")
            return

    old_val = ALERT_STATE.get(spec.key)
    ALERT_STATE[spec.key] = val
    save_settings()
    await update.message.reply_text(
        f"✅ **Setting Updated**\n\n**{spec.label}** modified:\n"
        f"`{_fmt(spec, old_val)}` ➡️ `{_fmt(spec, val)}`",
        parse_mode="Markdown"
    )


# ---------------------------------------------------------------- selections & toggles

@command("timeframe", "Change Analysis Timeframe Mode", icon="🕒", group="Strategy & Parameters", usage="/timeframe <mode>", order=2)
async def set_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or args[0].lower() not in TIMEFRAME_PRESETS:
        preset_lines = "\n".join(f"• `{k}` → {v['label']}" for k, v in TIMEFRAME_PRESETS.items())
        await update.message.reply_text(f"⚠️ **Usage:** `/timeframe <mode>`\n\n{preset_lines}", parse_mode="Markdown")
        return
    mode = args[0].lower()
    apply_timeframe_mode(mode)
    reset_runtime()
    save_settings()
    await update.message.reply_text(f"✅ Timeframe set to `{mode.upper()}` ({TIMEFRAME_PRESETS[mode]['label']})", parse_mode="Markdown")


@command("strategy", "Switch Active Trading Strategy", icon="⚙️", group="Strategy & Parameters", usage="/strategy [key|none]", order=1)
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
        reset_runtime()
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
    reset_runtime()
    save_settings()
    await update.message.reply_text(
        f"✅ Active strategy updated to `{choice.upper()}`\n📝 `{STRATEGY_PRESETS[choice]}`",
        parse_mode="Markdown"
    )


@command("filters", "Per-Strategy Hard Gates (structure/volume)", icon="🎛️", group="Strategy & Parameters",
         usage="/filters [strategy] [structure|volume] [on|off]", order=4)
async def filters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = [a.lower() for a in (context.args or [])]

    if not args:
        lines = ["🎛️ **HARD GATES (per strategy)**\n"]
        lines += [f"• `{key}` — {gates_summary(get_params(key))}" for key in STRATEGIES] or ["_No strategies installed._"]
        lines.append(
            "\n**Usage:** `/filters <strategy> structure|volume on|off`\n"
            "Same as `/set <strategy>.require_structure|require_volume on|off`.\n"
            "*The ATR/volume thresholds stay global (`atr_multiplier`, `volume_multiplier` in settings.json).*")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    if len(args) == 3 and args[0] in STRATEGIES and args[1] in _GATES and args[2] in ("on", "off"):
        skey, pkey = args[0], _GATES[args[1]]
        if not any(prm.key == pkey for prm in STRATEGIES[skey].params):
            await update.message.reply_text(f"❌ `{skey}` has no {args[1]} gate.", parse_mode="Markdown")
            return
        ALERT_STATE.setdefault("strategy_params", {}).setdefault(skey, {})[pkey] = (args[2] == "on")
        save_settings()
        await update.message.reply_text(f"✅ `{skey}` {args[1]} gate **{args[2].upper()}**", parse_mode="Markdown")
        return

    await update.message.reply_text("⚠️ Invalid format. Send `/filters` alone to see usage.", parse_mode="Markdown")


@command("watchlist", "Adjust Early Watchlist Pings", icon="👀", group="Strategy & Parameters", usage="/watchlist on|off", order=5)
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