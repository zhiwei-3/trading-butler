# bot/jobs/heartbeat.py
from datetime import datetime, timezone

from telegram.ext import ContextTypes

from config import ALERT_STATE
from bot.registry import job
from bot.messaging import send_text
from bot.jobs.status import build_status_snapshot


@job("heartbeat_ping", interval=lambda: ALERT_STATE["heartbeat_interval_hours"] * 3600, first=10,
     enabled=lambda: ALERT_STATE.get("heartbeat_enabled", False))
async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    if not ALERT_STATE.get("heartbeat_enabled"):
        return
    chat_id = context.job.chat_id
    if not chat_id:
        return
    snapshot = await build_status_snapshot()
    ALERT_STATE["last_heartbeat_at"] = datetime.now(timezone.utc)
    await send_text(context.bot, chat_id, f"💓 **HEARTBEAT — BOT IS ALIVE**\n\n{snapshot}")