# bot/jobs/news.py
from telegram.ext import ContextTypes

from config import ALERT_STATE, USER_ID
from bot.registry import job
from bot.messaging import send_text
from core.news_engine import upcoming_news_warnings, news_results_to_announce


@job("news_notifier", interval=60, first=8)
async def news_notifier_job(context: ContextTypes.DEFAULT_TYPE):
    """Sends the pre-release warnings and post-release results. Active while the scanner is on, as before."""
    if not ALERT_STATE.get("scanner_enabled", True):
        return
    chat_id = context.job.chat_id or USER_ID
    if not chat_id:
        return
    for msg in await upcoming_news_warnings():
        await send_text(context.bot, chat_id, msg)
    for msg in await news_results_to_announce():
        await send_text(context.bot, chat_id, msg)