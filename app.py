# app.py

import logging
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, ContextTypes
from config import TELEGRAM_TOKEN, USER_ID
from core.database import init_db
from core.mt5_engine import init_mt5
from bot.registry import install, bot_commands, start_all_jobs
import bot.commands   # noqa: F401  registers every @command / @callback
import bot.jobs       # noqa: F401  registers every @job


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs uncaught exceptions raised by command handlers."""
    if isinstance(context.error, (NetworkError, TimedOut)):
        logging.warning(f"⚠️ Network connection dropped (auto-retrying): {context.error}")
        return
    logging.error("Exception occurred while handling an update:", exc_info=context.error)


async def post_init_setup(application: Application) -> None:
    """Registers bot command auto-completion hints in the Telegram UI."""
    await application.bot.set_my_commands(bot_commands())


def main():
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN missing in environment.")
        return
    if not USER_ID:
        print("❌ TELEGRAM_USER_ID missing in environment — required for admin authorization and background jobs.")
        return

    init_db()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )
    app.post_init = post_init_setup

    if init_mt5():
        print("✅ MT5 Engine Online!")
    else:
        print("⚠️ Warning: MT5 connection failed — watchdog will keep retrying.")

    app.add_error_handler(error_handler)
    install(app)                                        # commands + callback dispatcher

    boot_id = int(USER_ID)
    start_all_jobs(app.job_queue, boot_id)              # scanner, outcome tracker, watchdog, heartbeat
    print(f"✅ Background jobs running for Chat ID: {boot_id}")

    print("🚀 Trading Butler running polling loop with error handling active!")
    app.run_polling()


if __name__ == '__main__':
    main()