# app.py

import logsetup  # noqa: F401  MUST stay the first import: configures file-only logging

import logging
import platform
from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, ContextTypes
from config import TELEGRAM_TOKEN, USER_ID, ALERT_STATE
from core.db import init_db, current_version
from core.mt5 import init_mt5
from bot.registry import install, bot_commands, start_all_jobs, JOBS, COMMANDS
from bot.audit import actor
from utils.logkit import system, kv
import bot.commands   # noqa: F401  registers every @command / @callback
import bot.jobs       # noqa: F401  registers every @job


def _startup(msg, level=logging.INFO):
    """Startup banner lines: shown in the terminal AND recorded in the log file."""
    print(msg)
    logging.log(level, msg)


def _log_startup_state():
    armed_live = bool(ALERT_STATE.get("auto_trade_enabled")) and not ALERT_STATE.get("trade_dry_run", True)
    system("startup", logging.WARNING if armed_live else logging.INFO,
           python=platform.python_version(), os=platform.platform(),
           strategy=ALERT_STATE.get("active_strategy"), timeframe=ALERT_STATE.get("timeframe_mode"),
           scanner=ALERT_STATE.get("scanner_enabled"), auto_trade=ALERT_STATE.get("auto_trade_enabled"),
           dry_run=ALERT_STATE.get("trade_dry_run"), LIVE_ARMED=armed_live or None,
           magic=ALERT_STATE.get("magic_number"), commands=len(COMMANDS), jobs=len(JOBS),
           log_level=logging.getLevelName(logging.getLogger().level))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs uncaught exceptions raised by command handlers and jobs, with who/what triggered them."""
    if isinstance(context.error, (NetworkError, TimedOut)):
        logging.warning(f"⚠️ Network connection dropped (auto-retrying): {context.error}")
        return
    ctx = {}
    if isinstance(update, Update):
        ctx.update(actor(update))
        cq, msg = update.callback_query, update.effective_message
        ctx["data"] = cq.data if cq else None
        ctx["text"] = msg.text if (msg and not cq) else None
    job = getattr(context, "job", None)
    if job is not None:
        ctx["job"] = job.name
    logging.error(f"Unhandled exception ({kv(**ctx) or 'no update context'}):", exc_info=context.error)


async def post_init_setup(application: Application) -> None:
    """Registers bot command auto-completion hints in the Telegram UI."""
    await application.bot.set_my_commands(bot_commands())


async def post_shutdown_log(application: Application) -> None:
    system("shutdown")


def main():
    if not TELEGRAM_TOKEN:
        _startup("❌ TELEGRAM_TOKEN missing in environment.", logging.CRITICAL)
        return
    if not USER_ID:
        _startup("❌ TELEGRAM_USER_ID missing in environment — required for admin authorization and background jobs.",
                 logging.CRITICAL)
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
    app.post_shutdown = post_shutdown_log

    if init_mt5():
        _startup("✅ MT5 Engine Online!")
    else:
        _startup("⚠️ Warning: MT5 connection failed — watchdog will keep retrying.", logging.WARNING)

    app.add_error_handler(error_handler)
    install(app)                                        # commands + callback dispatcher

    boot_id = int(USER_ID)
    start_all_jobs(app.job_queue, boot_id)              # scanner, outcome tracker, watchdog, heartbeat
    _startup(f"✅ Background jobs running for Chat ID: {boot_id}")

    _log_startup_state()
    _startup("🚀 Trading Butler running polling loop with error handling active!")
    app.run_polling()


if __name__ == '__main__':
    main()