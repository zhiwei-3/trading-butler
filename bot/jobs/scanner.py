# bot/jobs/scanner.py
import asyncio
import logging

from telegram.ext import ContextTypes

from config import ALERT_STATE, USER_ID
from bot.registry import job
from bot.messaging import send_text, send_alert
from bot.jobs.circuit_breaker import evaluate_circuit_breaker
from core.guards import GuardContext, run_guards
from core.mt5 import get_gold_symbol
from core.evaluator import analyze_market, evaluate_signals
from core.execution import execute_pending_intents


def _why(blocks):
    return "; ".join(f"{b.name}: {b.reason}" for b in blocks)


async def _scan_and_alert(context, chat_id, symbol):
    """Analysis → signal alerts + heads-up pings. Signal-scope guards suppress signals, never heads-ups."""
    try:
        analysis = await asyncio.to_thread(analyze_market, symbol)
        if not analysis:
            return
        blocks = await run_guards("signal", GuardContext(symbol, analysis))
        if blocks:
            logging.info(f"⏸️ Signals suppressed — {_why(blocks)}")
        signals_found, watch_found = await asyncio.to_thread(evaluate_signals, analysis, not blocks)
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

    if await evaluate_circuit_breaker(context):                 # side-effecting supervisor, not a pure gate
        return

    blocks = await run_guards("scan", GuardContext(symbol))     # news blockade, spread, ...
    if blocks:
        logging.info(f"⏸️ Scanner paused — {_why(blocks)}")
        return

    await _scan_and_alert(context, chat_id, symbol)
    await _run_executions(context, chat_id)