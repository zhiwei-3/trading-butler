import logging
from telegram.ext import Application, CommandHandler, ContextTypes
from config import ALERT_STATE, TELEGRAM_TOKEN, YOUR_CHAT_ID
from database import init_db
from mt5_engine import init_mt5
from bot.commands import *
from bot.jobs import (
    market_scanner_job, 
    signal_outcome_tracker_job, 
    restart_heartbeat_job, 
    ensure_watchdog_running
)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs uncaught exceptions raised by command handlers."""
    logging.error("Exception occurred while handling an update:", exc_info=context.error)

def main():
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN missing in environment.")
        return

    init_db()

    # Build app instance BEFORE calling app.job_queue
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    if init_mt5():
        print("✅ MT5 Engine Online!")
        if ALERT_STATE.get("heartbeat_enabled") and ALERT_STATE.get("heartbeat_chat_id"):
            restart_heartbeat_job(app.job_queue, int(ALERT_STATE["heartbeat_chat_id"]))
        if YOUR_CHAT_ID:
            ensure_watchdog_running(app.job_queue, int(YOUR_CHAT_ID))
    else:
        print("⚠️ Warning: MT5 connection failed.")

    # Register Global Error Handler
    app.add_error_handler(error_handler)

    # Register All Command Handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("scanner_on", enable_scanner))
    app.add_handler(CommandHandler("scanner_off", disable_scanner))
    app.add_handler(CommandHandler("news", news_calendar_cmd))
    app.add_handler(CommandHandler("spread", spread_check_cmd))
    app.add_handler(CommandHandler("calc", calc_risk))
    app.add_handler(CommandHandler("gold", gold_snapshot))
    app.add_handler(CommandHandler("session", market_session))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("timeframe", set_timeframe))
    app.add_handler(CommandHandler("strategy", set_strategy_cmd))
    app.add_handler(CommandHandler("filters", filters_cmd))
    app.add_handler(CommandHandler("confluence", confluence_cmd))
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("heartbeat", heartbeat_cmd))
    app.add_handler(CommandHandler("diagnose", diagnose_cmd))
    app.add_handler(CommandHandler("backtest", backtest_cmd))

    # Auto-start Background Jobs
    if YOUR_CHAT_ID:
        boot_id = int(YOUR_CHAT_ID)
        app.job_queue.run_repeating(
            market_scanner_job, 
            interval=60, 
            first=5, 
            chat_id=boot_id, 
            job_kwargs={"misfire_grace_time": 30}
        )
        app.job_queue.run_repeating(
            signal_outcome_tracker_job, 
            interval=30, 
            first=10, 
            chat_id=boot_id, 
            job_kwargs={"misfire_grace_time": 30}
        )
        print(f"✅ Background jobs running for Chat ID: {boot_id}")

    print("🚀 Trading Butler running polling loop with error handling active!")
    app.run_polling()

if __name__ == '__main__':
    main()