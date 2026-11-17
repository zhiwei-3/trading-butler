# bot/jobs/circuit_breaker.py
import asyncio

from telegram.ext import ContextTypes

from config import ALERT_STATE, USER_ID, save_settings
from bot.registry import stop_job
from bot.messaging import send_text
from core.database import get_daily_performance_stats
from core.trade_engine import close_all_managed


async def evaluate_circuit_breaker(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Evaluates daily performance. If thresholds are breached, shuts down the scanner
    and broadcasts an emergency Telegram alert. Returns True when it tripped.
    """
    if not ALERT_STATE.get("circuit_breaker_enabled", True):
        return False

    stats = await asyncio.to_thread(get_daily_performance_stats)
    max_losses = ALERT_STATE.get("max_daily_losses", 3)
    max_drawdown = ALERT_STATE.get("max_daily_drawdown_r", 3.0)

    reason = ""
    if stats["consecutive_losses"] >= max_losses:
        reason = f"Hit **{stats['consecutive_losses']} consecutive losses** today (Limit: `{max_losses}`)."
    elif stats["net_r"] <= -abs(max_drawdown):
        reason = f"Daily drawdown reached **{stats['net_r']:.1f}R** (Limit: `-{abs(max_drawdown):.1f}R`)."

    if not reason or not ALERT_STATE.get("scanner_enabled"):
        return False

    ALERT_STATE["scanner_enabled"] = False
    save_settings()
    stop_job(context.job_queue, "xauusd_scanner")

    flatten_note = ""
    if ALERT_STATE.get("flatten_on_circuit_breaker", True):
        results = await asyncio.to_thread(close_all_managed)
        flatten_note = "\n\n🧯 **Flattened managed positions:**\n" + "\n".join(f"• {r}" for r in results)

    msg = (
        f"🚨 **CIRCUIT BREAKER TRIGGERED — SCANNER DEACTIVATED** 🚨\n\n"
        f"• **Reason:** {reason}\n"
        f"• **Today's Net R:** `{stats['net_r']:.1f}R`\n"
        f"• **Today's Record:** `{stats['today_wins']}W - {stats['today_losses']}L`\n\n"
        "🛡️ *Scanner has been paused to protect capital. Review market conditions before re-enabling.*"
        f"{flatten_note}"
    )
    chat_id = context.job.chat_id if (context.job and context.job.chat_id) else int(USER_ID)
    if chat_id:
        await send_text(context.bot, chat_id, msg)
    return True