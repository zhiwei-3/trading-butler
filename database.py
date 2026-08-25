import sqlite3
from datetime import datetime, timezone
from config import DB_FILE

def init_db():
    """Creates the SQLite database and signals tracking table."""
    conn = sqlite3.connect(DB_FILE)
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
    conn.close()

def log_signal_to_db(symbol, direction, entry_price, sl_price, tp1_price, tp2_price, score):
    """Records a new trading signal for forward testing."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    timestamp = datetime.now(timezone.utc).isoformat()
    cursor.execute('''
        INSERT INTO signals (timestamp, symbol, direction, entry_price, sl_price, tp1_price, tp2_price, score, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
    ''', (timestamp, symbol, direction, entry_price, sl_price, tp1_price, tp2_price, score))
    conn.commit()
    conn.close()

def get_signal_stats():
    """Queries current win/loss outcomes for the /stats command."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT status, COUNT(*) FROM signals GROUP BY status")
    counts = dict(cursor.fetchall())
    conn.close()
    return counts