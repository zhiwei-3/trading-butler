from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, TIMEFRAME_PRESETS, STRATEGY_PRESETS, save_settings
from ._common import admin_only
from .core import menu_cmd, enable_scanner, disable_scanner, stats_cmd
from .market import gold_snapshot, spread_check_cmd


@admin_only
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "btn_scanner_on":
        await enable_scanner(update, context)
    elif data == "btn_scanner_off":
        await disable_scanner(update, context)
    elif data == "btn_gold":
        await gold_snapshot(update, context)
    elif data == "btn_spread":
        await spread_check_cmd(update, context)
    elif data == "btn_stats":
        await stats_cmd(update, context)
    elif data == "btn_strat_menu":
        # Built live from STRATEGY_PRESETS so the menu can never drift from it.
        keyboard = [[InlineKeyboardButton(v, callback_data=f"set_strat_{k}")] for k, v in STRATEGY_PRESETS.items()]
        keyboard.append([InlineKeyboardButton("« Back to Menu", callback_data="btn_main_menu")])
        await query.edit_message_text("⚙️ **Select Active Strategy:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    elif data.startswith("set_strat_"):
        new_strat = data.replace("set_strat_", "")
        ALERT_STATE["active_strategy"] = new_strat
        ALERT_STATE["last_rsi_signal"] = None
        save_settings()
        await menu_cmd(update, context)
    elif data == "btn_tf_menu":
        # Built from the real preset labels so they can never drift apart.
        keyboard = [[InlineKeyboardButton(v["label"], callback_data=f"set_tf_{k}")] for k, v in TIMEFRAME_PRESETS.items()]
        keyboard.append([InlineKeyboardButton("« Back to Menu", callback_data="btn_main_menu")])
        await query.edit_message_text("🕒 **Select Timeframe Preset:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    elif data.startswith("set_tf_"):
        mode = data.replace("set_tf_", "")
        preset = TIMEFRAME_PRESETS.get(mode)
        if preset:
            ALERT_STATE["timeframe_mode"] = mode
            ALERT_STATE["entry_tf"] = preset["entry"]
            ALERT_STATE["trend_tf"] = preset["trend"]
            ALERT_STATE["macro_tf"] = preset["macro"]
            ALERT_STATE["last_rsi_signal"] = None
            save_settings()
        await menu_cmd(update, context)
    elif data == "btn_main_menu":
        await menu_cmd(update, context)