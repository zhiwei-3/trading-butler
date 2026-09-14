import logging
import sqlite3
from datetime import datetime, timezone
from config import DB_FILE
from contextlib import contextmanager

@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                symbol TEXT,
                direction TEXT,
                entry_price REAL,
                sl_price REAL,
                tp1_price REAL,
                tp2_price REAL,
                score INTEGER,
                status TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("PRAGMA table_info(signals)")
        columns = [column[1] for column in cursor.fetchall()]
        for col, coltype in [("symbol", "TEXT"), ("score", "INTEGER"), ("updated_at", "DATETIME DEFAULT CURRENT_TIMESTAMP")]:
            if col not in columns:
                cursor.execute(f"ALTER TABLE signals ADD COLUMN {col} {coltype}")
        conn.commit()

def log_signal_to_db(symbol, direction, entry_price, sl_price, tp1_price, tp2_price, score):
    """Logs generated signal details to SQLite."""
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO signals (timestamp, symbol, direction, entry_price, sl_price, tp1_price, tp2_price, score, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
        ''', (timestamp, symbol, direction, entry_price, sl_price, tp1_price, tp2_price, score))
        conn.commit()

def get_signal_stats():
    """Retrieves current signal outcome statistics."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) FROM signals GROUP BY status")
        counts = dict(cursor.fetchall())
    return counts

def get_daily_performance_stats():
    """
    Calculates today's closed trade performance (UTC day) from the database.
    Returns dict: {'consecutive_losses': int, 'net_r': float, 'today_wins': int, 'today_losses': int}
    """
    today_start = datetime.now(timezone.utc).strftime('%Y-%m-%d 00:00:00')

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status FROM signals "
            "WHERE status IN ('HIT_TP1', 'HIT_TP2', 'CLOSED_BE', 'HIT_SL') "
            "AND updated_at >= ? ORDER BY id ASC",
            (today_start,)
        )
        rows = cursor.fetchall()

        consecutive_losses = 0
        net_r = 0.0
        today_wins = 0
        today_losses = 0

        for (status,) in rows:
            if status == 'HIT_SL':
                today_losses += 1
                consecutive_losses += 1
                net_r -= 1.0  # -1R per Stop Loss
            elif status == 'HIT_TP1':
                today_wins += 1
                consecutive_losses = 0
                net_r += 1.0  # +1R for TP1
            elif status == 'HIT_TP2':
                today_wins += 1
                consecutive_losses = 0
                net_r += 2.0  # +2R for TP2
            elif status == 'CLOSED_BE':
                today_wins += 1
                consecutive_losses = 0  # 0R for Break-Even

        return {
            "consecutive_losses": consecutive_losses,
            "net_r": net_r,
            "today_wins": today_wins,
            "today_losses": today_losses
        }