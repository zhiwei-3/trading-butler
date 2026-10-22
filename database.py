# database.py

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
            ("dual_entry", "INTEGER DEFAULT 0"),
            ("ticket", "INTEGER"),
            ("lots", "REAL"),
            ("exec_mode", "TEXT DEFAULT 'NONE'"),
            ("fill_price", "REAL"),
            ("ticket2", "INTEGER"),
            ("lots2", "REAL"),
            ("exec_mode2", "TEXT DEFAULT 'NONE'"),
            ("fill_price2", "REAL"),
            ("be_moved2", "INTEGER DEFAULT 0"),
            ("zone_low", "REAL"),
            ("zone_high", "REAL"),
            ("zone_type", "TEXT"),
        ]
        for col_name, col_type in migrations:
            if col_name not in columns:
                if col_name == "updated_at":
                    cursor.execute("ALTER TABLE signals ADD COLUMN updated_at DATETIME")
                    cursor.execute("UPDATE signals SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP)")
                else:
                    cursor.execute(f"ALTER TABLE signals ADD COLUMN {col_name} {col_type}")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_ticket ON signals(ticket)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_ticket2 ON signals(ticket2)")

        # --- optimization sweep history ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sweep_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                symbol TEXT,
                timeframe_mode TEXT,
                days INTEGER,
                rank_mode TEXT,
                swept_strategies INTEGER DEFAULT 0,
                swept_sl INTEGER DEFAULT 0,
                swept_rsi INTEGER DEFAULT 0,
                swept_rrr INTEGER DEFAULT 0,
                swept_score INTEGER DEFAULT 0,
                min_sample INTEGER,
                elapsed_seconds REAL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sweep_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES sweep_runs(id),
                rank INTEGER,
                strategy TEXT,
                min_rrr REAL,
                min_confluence_score INTEGER,
                sl_atr_mult REAL,
                rsi_buy REAL,
                rsi_sell REAL,
                total_trades INTEGER,
                wins INTEGER,
                losses INTEGER,
                open_trades INTEGER,
                win_rate REAL,
                avg_r REAL,
                net_r REAL,
                max_drawdown_r REAL,
                calmar REAL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sweep_results_run ON sweep_results(run_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sweep_runs_chat ON sweep_runs(chat_id)")

        conn.commit()

def log_signal_to_db(symbol, direction, entry_price, sl_price, tp1_price, tp2_price, score,
                     dual_entry=False, near_zone=None):
    """Logs generated signal details to SQLite. Returns the new row id (or None)."""
    timestamp = datetime.now(timezone.utc).isoformat()
    zone_low = zone_high = zone_type = None
    if near_zone:
        zone_low, zone_high, zone_type = near_zone.get("low"), near_zone.get("high"), near_zone.get("type")
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO signals (timestamp, symbol, direction, entry_price, sl_price, tp1_price, tp2_price,
                                     score, status, dual_entry, zone_low, zone_high, zone_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
            ''', (timestamp, symbol, direction, entry_price, sl_price, tp1_price, tp2_price, score,
                  int(bool(dual_entry)), zone_low, zone_high, zone_type))
            conn.commit()
            return cursor.lastrowid
    except Exception as e:
        logging.error(f"⚠️ Failed to log signal to DB: {e}")
        return None

def attach_trade_execution(signal_id, leg, ticket, lots, exec_mode, fill_price=None):
    """Links a live/dry MT5 fill back onto its originating signal row."""
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
    """Live fills (tickets) opened since 00:00 UTC."""
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
    """Today's CLOSED trade performance (UTC day). R is computed from each signal's real levels,
    using the same model as the backtester (dual: TP1/2 leg-averages; BE runner = tp1_r/2)."""
    today_start = datetime.now(timezone.utc).strftime('%Y-%m-%d 00:00:00')
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT status, entry_price, sl_price, tp1_price, tp2_price FROM signals "
            "WHERE status IN ('CLOSED_TP1', 'HIT_TP2', 'CLOSED_BE', 'HIT_SL') "
            "AND updated_at >= ? ORDER BY updated_at ASC, id ASC",
            (today_start,)
        ).fetchall()

    consecutive_losses, net_r, today_wins, today_losses = 0, 0.0, 0, 0
    for row in rows:
        sl_dist = abs(row["entry_price"] - row["sl_price"])
        tp1_r = abs(row["tp1_price"] - row["entry_price"]) / sl_dist if sl_dist > 0 else 0.0
        tp2_r = abs(row["tp2_price"] - row["entry_price"]) / sl_dist if sl_dist > 0 else 0.0
        status = row["status"]

        if status == 'HIT_SL':
            today_losses += 1
            consecutive_losses += 1
            net_r -= 1.0
            continue

        today_wins += 1
        consecutive_losses = 0
        if status == 'CLOSED_TP1':
            net_r += tp1_r
        elif status == 'HIT_TP2':
            net_r += (tp1_r + tp2_r) / 2
        elif status == 'CLOSED_BE':
            net_r += tp1_r / 2

    return {
        "consecutive_losses": consecutive_losses,
        "net_r": round(net_r, 2),
        "today_wins": today_wins,
        "today_losses": today_losses,
    }

def get_open_dry_run_signals():
    """Open simulated (dry-run) trades only: signals with at least one DRY leg still running.
    Alert-only signals (exec_mode 'NONE') are NOT positions and are excluded."""
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, created_at, symbol, direction, entry_price, sl_price, "
            "       tp1_price, tp2_price, score, status, dual_entry, ticket, lots, "
            "       exec_mode, fill_price, ticket2, lots2, exec_mode2, fill_price2, be_moved2 "
            "FROM signals "
            "WHERE status IN ('PENDING', 'HIT_TP1') "
            "  AND (exec_mode = 'DRY' OR exec_mode2 = 'DRY') "
            "ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

def get_active_live_signals():
    """Active signals that own live tickets, oldest first."""
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, ticket, ticket2 FROM signals "
            "WHERE status IN ('PENDING','HIT_TP1') "
            "AND ((exec_mode='LIVE' AND ticket IS NOT NULL) OR (exec_mode2='LIVE' AND ticket2 IS NOT NULL)) "
            "ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

def get_open_executed_signals(exclude_id=None):
    """Signals that own an open (live OR dry-run) trade. Used so the one-trade
    rule also holds in dry-run, where no real MT5 position exists."""
    query = (
        "SELECT id, exec_mode, exec_mode2 FROM signals "
        "WHERE status IN ('PENDING','HIT_TP1') "
        "AND (exec_mode IN ('DRY','LIVE') OR exec_mode2 IN ('DRY','LIVE'))"
    )
    params = []
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    query += " ORDER BY id ASC"
    with get_db_connection() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_dry_run_journal_signals():
    """Every signal that owns at least one dry-run leg (open or closed). Alert-only
    and SKIPPED signals never have exec_mode 'DRY', so they are excluded."""
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, updated_at, symbol, direction, entry_price, sl_price, "
            "       tp1_price, tp2_price, score, status, dual_entry, "
            "       lots, exec_mode, fill_price, lots2, exec_mode2, fill_price2 "
            "FROM signals "
            "WHERE status != 'SKIPPED' AND (exec_mode = 'DRY' OR exec_mode2 = 'DRY') "
            "ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_live_signal_links():
    """Maps live position tickets back to the signal that opened them: {ticket: {id, leg, score}}."""
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, score, ticket, ticket2, exec_mode, exec_mode2 FROM signals "
            "WHERE (exec_mode = 'LIVE' AND ticket IS NOT NULL) "
            "   OR (exec_mode2 = 'LIVE' AND ticket2 IS NOT NULL)"
        ).fetchall()
    links = {}
    for r in rows:
        if r["exec_mode"] == "LIVE" and r["ticket"]:
            links[int(r["ticket"])] = {"id": r["id"], "leg": 1, "score": r["score"]}
        if r["exec_mode2"] == "LIVE" and r["ticket2"]:
            links[int(r["ticket2"])] = {"id": r["id"], "leg": 2, "score": r["score"]}
    return links
    

def get_recent_trades_for_chart(limit=30, symbol=None):
    """Recent signals (open and closed) for overlaying on charts. Times are true UTC.
    SKIPPED signals (never executed) are excluded."""
    query = """
        SELECT
            id, symbol, direction, dual_entry,
            timestamp as time, timestamp as entry_time,
            entry_price as entry, entry_price,
            sl_price as sl, sl_price,
            tp1_price as tp1, tp1_price,
            tp2_price as tp2, tp2_price,
            status as outcome, status,
            updated_at as closed_at
        FROM signals
        WHERE status != 'SKIPPED'
    """
    params = []
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

def log_sweep_run(chat_id, symbol, timeframe_mode, days, rank_mode, swept_flags, min_sample, elapsed_seconds, rows):
    """Persists a completed /optimize sweep run and its detailed results."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO sweep_runs
                (chat_id, symbol, timeframe_mode, days, rank_mode,
                 swept_strategies, swept_sl, swept_rsi, swept_rrr, swept_score,
                 min_sample, elapsed_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                chat_id, symbol, timeframe_mode, days, rank_mode,
                int(swept_flags.get("strategies", False)), int(swept_flags.get("sl", False)),
                int(swept_flags.get("rsi", False)), int(swept_flags.get("rrr", False)),
                int(swept_flags.get("score", False)), min_sample, elapsed_seconds,
            ))
            run_id = cursor.lastrowid

            for i, row in enumerate(rows, start=1):
                rsi_pair = row.get("rsi_pair", (None, None))
                cursor.execute("""
                    INSERT INTO sweep_results
                    (run_id, rank, strategy, min_rrr, min_confluence_score, sl_atr_mult,
                     rsi_buy, rsi_sell, total_trades, wins, losses, open_trades,
                     win_rate, avg_r, net_r, max_drawdown_r, calmar)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    run_id, i, row.get("strategy"), row.get("min_rrr"), row.get("min_confluence_score"),
                    row.get("sl_atr_mult"), rsi_pair[0], rsi_pair[1],
                    row.get("total_trades", 0), row.get("wins", 0), row.get("losses", 0), row.get("open", 0),
                    row.get("win_rate"), row.get("avg_r"), row.get("net_r"), row.get("max_drawdown_r"),
                    row.get("calmar"),
                ))
            conn.commit()
            return run_id
    except Exception as e:
        logging.error(f"⚠️ Failed to log sweep run to DB: {e}")
        return None

def get_sweep_run(run_id):
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM sweep_runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

def get_latest_sweep_run(chat_id):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM sweep_runs WHERE chat_id = ? ORDER BY id DESC LIMIT 1", (chat_id,)
        ).fetchone()
        return dict(row) if row else None

def get_sweep_results(run_id, limit=None):
    query = "SELECT * FROM sweep_results WHERE run_id = ? ORDER BY rank ASC"
    params = [run_id]
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    with get_db_connection() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]

def count_sweep_runs(chat_id):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sweep_runs WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

def get_sweep_history(chat_id, limit=10, offset=0):
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM sweep_runs WHERE chat_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (chat_id, limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]

def get_sweep_history_ranked(chat_id, sort_column="net_r", sort_dir="DESC", limit=10, offset=0):
    with get_db_connection() as conn:
        query = f"""
            SELECT sr.*, res.strategy AS top_strategy, res.net_r AS top_net_r, res.avg_r AS top_avg_r,
                   res.win_rate AS top_win_rate, res.wins AS top_wins, res.losses AS top_losses,
                   res.total_trades AS top_trades, res.max_drawdown_r AS top_dd, res.calmar AS top_calmar
            FROM sweep_runs sr
            JOIN sweep_results res ON res.run_id = sr.id AND res.rank = 1
            WHERE sr.chat_id = ?
            ORDER BY res.{sort_column} {sort_dir}
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(query, (chat_id, limit, offset)).fetchall()
        return [dict(r) for r in rows]