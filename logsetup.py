# logsetup.py
"""
File-only logging. Import this FIRST (before config / strategies), so warnings emitted
at import time (strategy discovery, settings load) are captured too.
No console handler is attached: nothing is printed to the terminal.
"""

import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "trading_butler.log")


def setup_logging(level=logging.INFO):
    os.makedirs(LOG_DIR, exist_ok=True)

    handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5,
                                  encoding="utf-8")       # utf-8: the messages contain emojis
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.handlers.clear()                                  # drop any console/default handlers
    root.addHandler(handler)
    root.setLevel(level)

    # httpx INFO lines contain the bot token in the request URL; APScheduler is chatty.
    for noisy in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Uncaught exceptions (main thread and worker threads) go to the file instead of the console.
    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _hook
    threading.excepthook = lambda a: logging.critical(
        f"Uncaught exception in thread {a.thread.name if a.thread else '?'}",
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


setup_logging()