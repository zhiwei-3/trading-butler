import logging
from telegram.ext import Application, CommandHandler, ContextTypes
from config import TELEGRAM_TOKEN, YOUR_CHAT_ID
from database import init_db
from mt5_engine import init_mt5
from bot.commands import (
    start_cmd, enable_scanner, disable_scanner, news_calendar_cmd,
    spread_check_cmd, calc_risk, gold_snapshot, market_session,
    set_timeframe, filters_cmd, confluence_cmd, watchlist_cmd,
    heartbeat_cmd, status_cmd, diagnose_cmd, stats_cmd
)
from bot.jobs import market_scanner_job, signal_outcome_tracker_job

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs uncaught exceptions raised by command handlers."""
    logging.error("Exception occurred while handling an update:", exc_info=context.error)

def main():
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN missing in environment.")
        return

    init_db()
    if init_mt5():
        print("✅ MT5 Engine Online!")
    else:
        print("⚠️ Warning: MT5 connection failed.")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

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
    app.add_handler(CommandHandler("timeframe", set_timeframe))
    app.add_handler(CommandHandler("filters", filters_cmd))
    app.add_handler(CommandHandler("confluence", confluence_cmd))
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("heartbeat", heartbeat_cmd))
    app.add_handler(CommandHandler("diagnose", diagnose_cmd))

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