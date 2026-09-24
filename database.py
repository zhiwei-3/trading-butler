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
        migrations = [
            ("symbol", "TEXT"),
            ("score", "INTEGER"),
            ("updated_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
            # --- live execution linkage ---
            # Two-leg model: "leg 1" always targets TP1 (full close there — this is
            # the only leg a single-entry / low-score signal ever gets). "leg 2"
            # only exists for dual-entry (high-score smc_confluence) signals: it
            # targets TP2 and has its SL trailed to break-even the moment leg 1
            # closes at TP1.
            ("dual_entry", "INTEGER DEFAULT 0"),
            ("ticket", "INTEGER"),          # leg 1 (TP1) ticket
            ("lots", "REAL"),               # leg 1 lots
            ("exec_mode", "TEXT DEFAULT 'NONE'"),      # NONE | DRY | LIVE
            ("fill_price", "REAL"),
            ("ticket2", "INTEGER"),         # leg 2 (TP2 runner) ticket — NULL for single-entry
            ("lots2", "REAL"),
            ("exec_mode2", "TEXT DEFAULT 'NONE'"),
            ("fill_price2", "REAL"),
            ("be_moved2", "INTEGER DEFAULT 0"),        # leg 2 SL has been trailed to break-even
            # Superseded by the two-leg columns above (kept only so old rows/DBs
            # don't break); the old single-ticket partial-close model no longer
            # writes to these.
            ("tp1_partial_done", "INTEGER DEFAULT 0"),
            ("be_moved", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_type in migrations:
            if col_name not in columns:
                cursor.execute(f"ALTER TABLE signals ADD COLUMN {col_name} {col_type}")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_ticket ON signals(ticket)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_ticket2 ON signals(ticket2)")
        conn.commit()

def log_signal_to_db(symbol, direction, entry_price, sl_price, tp1_price, tp2_price, score, dual_entry=False):
    """Logs generated signal details to SQLite. Returns the new row id (or None)."""
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO signals (timestamp, symbol, direction, entry_price, sl_price, tp1_price, tp2_price, score, status, dual_entry)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
            ''', (timestamp, symbol, direction, entry_price, sl_price, tp1_price, tp2_price, score, int(bool(dual_entry))))
            conn.commit()
            return cursor.lastrowid
    except Exception as e:
        logging.error(f"⚠️ Failed to log signal to DB: {e}")
        return None

def attach_trade_execution(signal_id, leg, ticket, lots, exec_mode, fill_price=None):
    """Links a live/dry MT5 fill back onto its originating signal row.
    leg=1 -> the TP1 leg (every signal has this one). leg=2 -> the TP2 runner
    leg (only present on dual-entry signals)."""
    if not signal_id:
        return
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    LEG_COLUMNS = {
      1: ("ticket", "lots", "exec_mode", "fill_price"),
      2: ("ticket2", "lots2", "exec_mode2", "fill_price2"),
  }
    cols = " = ?, ".join(LEG_COLUMNS[leg]) + " = ?"
    try:
        with get_db_connection() as conn:
            conn.execute(
                f"UPDATE signals SET {cols}, updated_at = ? WHERE id = ?",
                (ticket, lots, exec_mode, fill_price, now_str, signal_id)
            )
            conn.commit()
    except Exception as e:
        logging.error(f"⚠️ Failed to attach leg {leg} execution to signal {signal_id}: {e}")

def mark_leg2_be_moved(signal_id):
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    if not signal_id:
        return
    try:
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE signals SET be_moved2 = 1, updated_at = ? WHERE id = ?",
                (now_str, signal_id)
            )
            conn.commit()
    except Exception as e:
        logging.error(f"⚠️ Failed to mark be_moved2 for signal {signal_id}: {e}")

def count_live_trades_today():
    """Live fills (tickets) opened since 00:00 UTC — feeds the daily execution cap.
    A dual-entry signal contributes up to 2 to this count (one per leg), since each
    leg is its own real order sent to the broker."""
    today_start = datetime.now(timezone.utc).strftime('%Y-%m-%d 00:00:00')
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT "
                "  SUM(CASE WHEN exec_mode = 'LIVE' AND ticket IS NOT NULL THEN 1 ELSE 0 END) + "
                "  SUM(CASE WHEN exec_mode2 = 'LIVE' AND ticket2 IS NOT NULL THEN 1 ELSE 0 END) AS n "
                "FROM signals WHERE created_at >= ?",
                (today_start,)
            ).fetchone()
        return int(row["n"] or 0) if row else 0
    except Exception:
        return 0

def set_signal_status(signal_id, status):
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with get_db_connection() as conn:
        conn.execute("UPDATE signals SET status = ?, updated_at = ? WHERE id = ?", (status, now_str, signal_id))
        conn.commit()

def get_signal_stats():
    """Retrieves current signal outcome statistics."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) AS count FROM signals GROUP BY status")
        counts = {row["status"]: row["count"] for row in cursor.fetchall()}
    return counts

def get_daily_performance_stats():
    """
    Calculates today's CLOSED trade performance (UTC day) from the database.

    Status vocabulary under the two-leg model:
      PENDING     - nothing has closed yet
      HIT_TP1     - (dual-entry only) leg 1 closed at TP1, leg 2 still running to
                    TP2/BE. NOT a closed outcome — stays excluded from this query,
                    same reasoning as before: counting it here would double-count
                    once leg 2's real outcome also lands, or mask a real drawdown.
      CLOSED_TP1  - (single-entry only) the ONE leg closed fully at TP1. This IS
                    a terminal, closed outcome — included below.
      HIT_TP2     - (dual-entry only) leg 2 hit TP2. Terminal win.
      CLOSED_BE   - (dual-entry only) leg 2 got stopped at break-even after its
                    SL was trailed. Terminal, ~0R.
      HIT_SL      - either leg (or both) got stopped at the original SL. Terminal
                    loss.
    """
    today_start = datetime.now(timezone.utc).strftime('%Y-%m-%d 00:00:00')

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status FROM signals "
            "WHERE status IN ('CLOSED_TP1', 'HIT_TP2', 'CLOSED_BE', 'HIT_SL') "
            "AND updated_at >= ? ORDER BY id ASC",
            (today_start,)
        )
        rows = cursor.fetchall()

        consecutive_losses = 0
        net_r = 0.0
        today_wins = 0
        today_losses = 0

        for row in rows:
            status = row["status"]
            if status == 'HIT_SL':
                today_losses += 1
                consecutive_losses += 1
                net_r -= 1.0
            elif status == 'HIT_TP2':
                today_wins += 1
                consecutive_losses = 0
                net_r += 2.0
            elif status == 'CLOSED_TP1':
                today_wins += 1
                consecutive_losses = 0
                net_r += 1.0
            elif status == 'CLOSED_BE':
                today_wins += 1
                consecutive_losses = 0

        return {
            "consecutive_losses": consecutive_losses,
            "net_r": net_r,
            "today_wins": today_wins,
            "today_losses": today_losses
        }
