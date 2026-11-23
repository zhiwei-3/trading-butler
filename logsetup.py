# logsetup.py
"""
File-only logging. Import this FIRST (before config / strategies), so warnings emitted
at import time (strategy discovery, settings load) are captured too.
No console handler is attached: nothing is printed to the terminal.

Files (logs/):
  trading_butler.log   everything >= LOG_LEVEL (env, default INFO)
  errors.log           WARNING and above only
  activity.log         user activity (tb.audit) + trade lifecycle (tb.trade), rotated daily
"""

import logging
import os
import re
import sys
import threading
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

from dotenv import load_dotenv

load_dotenv()      # so LOG_LEVEL / TELEGRAM_TOKEN are visible before config.py runs

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "trading_butler.log")
ERROR_FILE = os.path.join(LOG_DIR, "errors.log")
ACTIVITY_FILE = os.path.join(LOG_DIR, "activity.log")

_DATEFMT = "%Y-%m-%d %H:%M:%S"
_MAIN_FMT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_ACTIVITY_FMT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s %(message)s"

_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")      # Telegram bot token shape


class _SafeFormatter(logging.Formatter):
    """Redacts the bot token from the FINAL text (messages, tracebacks, request URLs)."""
    def format(self, record):
        text = super().format(record)
        secret = os.getenv("TELEGRAM_TOKEN")
        if secret:
            text = text.replace(secret, "<token>")
        return _TOKEN_RE.sub("<token>", text)


class _Prefix(logging.Filter):
    def __init__(self, *prefixes):
        super().__init__()
        self.prefixes = prefixes

    def filter(self, record):
        return record.name.startswith(self.prefixes)


def _level():
    return getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)


def setup_logging(level=None):
    level = level or _level()
    os.makedirs(LOG_DIR, exist_ok=True)

    main = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    main.setFormatter(_SafeFormatter(_MAIN_FMT, _DATEFMT))

    errors = RotatingFileHandler(ERROR_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    errors.setLevel(logging.WARNING)
    errors.setFormatter(_SafeFormatter(_MAIN_FMT, _DATEFMT))

    activity = TimedRotatingFileHandler(ACTIVITY_FILE, when="midnight", backupCount=30, encoding="utf-8")
    activity.addFilter(_Prefix("tb.audit", "tb.trade"))
    activity.setFormatter(_SafeFormatter(_ACTIVITY_FMT, _DATEFMT))

    root = logging.getLogger()
    root.handlers.clear()                                  # drop any console/default handlers
    for h in (main, errors, activity):
        root.addHandler(h)
    root.setLevel(level)

    # Chatty / sensitive libraries (httpx INFO lines contain the bot token in the URL).
    for noisy in ("httpx", "httpcore", "apscheduler", "matplotlib", "PIL", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.captureWarnings(True)                          # pandas/numpy warnings -> log instead of lost stderr

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