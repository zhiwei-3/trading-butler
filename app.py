from telegram.ext import Application, CommandHandler
from config import TELEGRAM_TOKEN, YOUR_CHAT_ID
from database import init_db
from mt5_engine import init_mt5
from bot.commands import start_cmd, diagnose_cmd, stats_cmd, status_cmd
from bot.jobs import market_scanner_job, signal_outcome_tracker_job

def main():
    if not TELEGRAM_TOKEN:
        print("❌ Error: TELEGRAM_TOKEN missing in environment.")
        return

    init_db()
    if init_mt5():
        print("✅ MT5 Engine Online!")
    else:
        print("⚠️ Warning: MT5 connection failed.")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Register Handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("diagnose", diagnose_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    # Auto-start Background Jobs
    if YOUR_CHAT_ID:
        boot_id = int(YOUR_CHAT_ID)
        app.job_queue.run_repeating(market_scanner_job, interval=60, first=5, chat_id=boot_id)
        app.job_queue.run_repeating(signal_outcome_tracker_job, interval=30, first=10, chat_id=boot_id)
        print(f"✅ Scanner & Tracker background jobs scheduled for Chat ID: {boot_id}")

    print("🚀 Trading Butler running polling loop...")
    app.run_polling()

if __name__ == '__main__':
    main()