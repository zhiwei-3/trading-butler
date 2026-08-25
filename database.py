import sqlite3
from datetime import datetime, timezone
from config import DB_FILE

def get_db_connection():
    """Returns a SQLite connection configured with a 10-second busy timeout."""
    return sqlite3.connect(DB_FILE, timeout=10.0)

def init_db():
    """Creates the SQLite table for signal performance tracking."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
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
                status TEXT DEFAULT 'PENDING',
                closed_at TEXT
            )
        ''')
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