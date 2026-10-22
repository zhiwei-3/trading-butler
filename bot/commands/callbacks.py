from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import ALERT_STATE, TIMEFRAME_PRESETS, STRATEGY_PRESETS, save_settings
from ._common import admin_only
from .core import menu_cmd, enable_scanner, disable_scanner, account_cmd
from .trading import positions_cmd
from .market import gold_snapshot, spread_check_cmd


def _reset_dedup():
    """Switching strategy/timeframe must clear every dedup key, or the first signal
    on the new configuration can be silently suppressed."""
    ALERT_STATE["last_rsi_signal"] = None
    ALERT_STATE["last_ifvg_signal"] = None
    ALERT_STATE["last_watch_signal"] = None


@admin_only
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    try:
        if data == "btn_scanner_on":
            await enable_scanner(update, context)
        elif data == "btn_scanner_off":
            await disable_scanner(update, context)
        elif data == "btn_gold":
            await gold_snapshot(update, context)
        elif data == "btn_spread":
            await spread_check_cmd(update, context)
        elif data == "btn_account":
            await account_cmd(update, context)
        elif data == "btn_positions":
            await positions_cmd(update, context)
        elif data == "btn_strat_menu":
            keyboard = [[InlineKeyboardButton(v, callback_data=f"set_strat_{k}")] for k, v in STRATEGY_PRESETS.items()]
            keyboard.append([InlineKeyboardButton("« Back to Menu", callback_data="btn_main_menu")])
            await query.edit_message_text("⚙️ **Select Active Strategy:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        elif data.startswith("set_strat_"):
            new_strat = data.replace("set_strat_", "", 1)
            if new_strat in STRATEGY_PRESETS:
                ALERT_STATE["active_strategy"] = new_strat
                _reset_dedup()
                save_settings()
            await menu_cmd(update, context)
        elif data == "btn_tf_menu":
            keyboard = [[InlineKeyboardButton(v["label"], callback_data=f"set_tf_{k}")] for k, v in TIMEFRAME_PRESETS.items()]
            keyboard.append([InlineKeyboardButton("« Back to Menu", callback_data="btn_main_menu")])
            await query.edit_message_text("🕒 **Select Timeframe Preset:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        elif data.startswith("set_tf_"):
            mode = data.replace("set_tf_", "", 1)
            preset = TIMEFRAME_PRESETS.get(mode)
            if preset:
                ALERT_STATE["timeframe_mode"] = mode
                ALERT_STATE["entry_tf"] = preset["entry"]
                ALERT_STATE["trend_tf"] = preset["trend"]
                ALERT_STATE["macro_tf"] = preset["macro"]
                _reset_dedup()
                save_settings()
            await menu_cmd(update, context)
        elif data == "btn_main_menu":
            await menu_cmd(update, context)
    except BadRequest as e:
        # Re-selecting the current option re-renders an identical message.
        if "not modified" not in str(e).lower():
            raise