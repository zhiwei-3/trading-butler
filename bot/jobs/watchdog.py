# bot/jobs/watchdog.py
import asyncio

from telegram.ext import ContextTypes

from config import ALERT_STATE
from bot.registry import job
from bot.messaging import send_text
from core.mt5_engine import check_mt5_alive


@job("mt5_watchdog", interval=90, first=15)
async def mt5_watchdog_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    if not chat_id:
        return
    mt5_ok = await asyncio.to_thread(check_mt5_alive)
    was_ok = ALERT_STATE["mt5_connected"]

    if mt5_ok:
        ALERT_STATE["consecutive_mt5_failures"] = 0
        if not was_ok:
            ALERT_STATE["mt5_connected"] = True
            await send_text(context.bot, chat_id,
                            "✅ **MT5 CONNECTION RESTORED**\n\nThe bot has reconnected to MT5. Scanning and signals have resumed.")
        return

    ALERT_STATE["consecutive_mt5_failures"] += 1
    if was_ok and ALERT_STATE["consecutive_mt5_failures"] >= 2:
        ALERT_STATE["mt5_connected"] = False
        await send_text(context.bot, chat_id,
                        "🚨 **MT5 CONNECTION LOST!**\n\nThe bot can't reach the MT5 terminal. Signal generation is effectively paused.")