# bot/commands/menu.py
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, TIMEFRAME_PRESETS, save_settings, apply_timeframe_mode
from strategies import STRATEGY_PRESETS, get_strategy, get_params, reset_runtime, sizing_summary
from bot.registry import command, callback


@command("menu", "Interactive Control Dashboard", icon="🎛️", group="Core & Dashboards", usage="/menu", order=1)
@callback("btn_main_menu")
async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scanner_status = "🔴 OFF" if not ALERT_STATE.get("scanner_enabled") else "🟢 ON"
    tf_mode = ALERT_STATE.get("timeframe_mode", "scalp").upper()
    strat = get_strategy(ALERT_STATE.get("active_strategy"))
    strat_name = strat.key.replace("_", " ").upper() if strat else "NONE"
    strat_summary = strat.summary(get_params(strat.key)) if strat else "—"

    text = (
        "🎛️ **TRADING BUTLER CONTROL DASHBOARD**\n\n"
        f"• **Scanner:** `{scanner_status}`\n"
        f"• **Active Strategy:** `{strat_name}`\n"
        f"• **Settings:** `{strat_summary}`\n"
        f"• **Timeframe Preset:** `{tf_mode}`\n"
        f"• **Sizing:** `{sizing_summary(strat.key if strat else None)}`\n\n"
        "Tap a button below for instant quick actions:"
    )
    keyboard = [
        [
            InlineKeyboardButton("🟢 Scanner ON", callback_data="btn_scanner_on"),
            InlineKeyboardButton("🔴 Scanner OFF", callback_data="btn_scanner_off"),
        ],
        [
            InlineKeyboardButton("💳 Account", callback_data="btn_account"),
            InlineKeyboardButton("💼 Positions", callback_data="btn_positions"),
            InlineKeyboardButton("📓 Journal", callback_data="btn_journal"),
        ],
        [
            InlineKeyboardButton("📊 Gold Chart", callback_data="btn_gold"),
            InlineKeyboardButton("⚙️ Strategy Selector", callback_data="btn_strat_menu"),
            InlineKeyboardButton("🕒 Timeframe Selector", callback_data="btn_tf_menu"),
        ],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


@callback("btn_strat_menu")
async def strategy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if STRATEGY_PRESETS:
        title = "⚙️ **Select Active Strategy:**"
        keyboard = [[InlineKeyboardButton(v, callback_data=f"set_strat_{k}")] for k, v in STRATEGY_PRESETS.items()]
    else:
        title = "⚙️ **No strategies installed.**\nAdd a package to `strategies/` and restart."
        keyboard = []
    keyboard.append([InlineKeyboardButton("« Back to Menu", callback_data="btn_main_menu")])
    await update.callback_query.edit_message_text(title, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


@callback("set_strat_", prefix=True)
async def select_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new = update.callback_query.data.replace("set_strat_", "", 1)
    if new in STRATEGY_PRESETS:
        ALERT_STATE["active_strategy"] = new
        reset_runtime()
        save_settings()
    await menu_cmd(update, context)


@callback("btn_tf_menu")
async def timeframe_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(v["label"], callback_data=f"set_tf_{k}")] for k, v in TIMEFRAME_PRESETS.items()]
    keyboard.append([InlineKeyboardButton("« Back to Menu", callback_data="btn_main_menu")])
    await update.callback_query.edit_message_text("🕒 **Select Timeframe Preset:**",
                                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


@callback("set_tf_", prefix=True)
async def select_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = update.callback_query.data.replace("set_tf_", "", 1)
    if apply_timeframe_mode(mode):
        reset_runtime()
        save_settings()
    await menu_cmd(update, context)