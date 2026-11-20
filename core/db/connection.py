# core/db/connection.py
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config


@contextmanager
def get_db_connection():
    # config.DB_FILE is read at call time (not import time) so tests can point it at a temp file.
    conn = sqlite3.connect(config.DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        yield conn
    finally:
        conn.close()


def utc_now_str():
    """The DB's timestamp format: true UTC, 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')