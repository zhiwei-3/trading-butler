# bot/jobs/scanner.py
import asyncio
import logging

from telegram.ext import ContextTypes

from config import ALERT_STATE, USER_ID
from bot.registry import job
from bot.messaging import send_text, send_alert
from bot.jobs.circuit_breaker import evaluate_circuit_breaker
from core.mt5_engine import get_gold_symbol, get_tick
from core.news_engine import news_guard_check, check_news_blockade, check_news_post_release
from core.evaluator import analyze_market, evaluate_signals
from core.trade_engine import execute_pending_intents


async def _spread_ok(symbol) -> bool:
    tick = await asyncio.to_thread(get_tick, symbol)
    if not tick:
        return True
    spread_pips = round((tick.ask - tick.bid) * 10, 1)
    if spread_pips > ALERT_STATE["max_allowed_spread_pips"]:
        logging.info(f"⚠️ Scanner skipped: Spread too high ({spread_pips} pips)")
        return False
    return True


async def _scan_and_alert(context, chat_id, symbol):
    """Market technical evaluation → signal alerts + heads-up pings."""
    try:
        analysis = await asyncio.to_thread(analyze_market, symbol)
        if not analysis:
            return
        signals_found, watch_found = await asyncio.to_thread(evaluate_signals, analysis)
        for item in signals_found:
            msg_text, chart_buffer = item if isinstance(item, tuple) else (item, None)
            await send_alert(context.bot, chat_id, msg_text, chart_buffer)
        for watch_msg in watch_found:
            await send_text(context.bot, chat_id, watch_msg)
    except Exception:
        logging.exception("Scanner evaluation failed")


async def _run_executions(context, chat_id):
    """Drains queued execution intents (blocking MT5 order_send, kept off the event loop)."""
    try:
        reports = await asyncio.to_thread(execute_pending_intents)
    except Exception as e:
        logging.exception("Trade execution drain crashed")
        reports = [f"❌ **Execution engine error:** `{e}`"]
    for report in reports:
        await send_text(context.bot, chat_id, report)


@job("xauusd_scanner", interval=60, first=5, enabled=lambda: ALERT_STATE.get("scanner_enabled", True))
async def market_scanner_job(context: ContextTypes.DEFAULT_TYPE):
    if not ALERT_STATE["scanner_enabled"]:
        return
    chat_id = context.job.chat_id or USER_ID
    if not chat_id:
        return
    symbol = await asyncio.to_thread(get_gold_symbol) or "XAUUSD"

    if await evaluate_circuit_breaker(context):          # 1. circuit breaker
        return
    await news_guard_check(context, chat_id)             # 2. pre-event warning
    await check_news_post_release(context, chat_id)      # 3. post-release result broadcast

    is_blocked, reason = await check_news_blockade()     # 4. news blackout
    ALERT_STATE["news_lockout"] = is_blocked
    if is_blocked:
        logging.info(f"📰 Market scanner paused due to news blockade: {reason}")
        return

    if not await _spread_ok(symbol):                     # 5. spread guard
        return

    await _scan_and_alert(context, chat_id, symbol)      # 6. technical evaluation
    await _run_executions(context, chat_id)              # 7. live execution drain