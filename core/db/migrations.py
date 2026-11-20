# core/db/migrations.py
"""
Versioned schema migrations, tracked with SQLite's built-in `PRAGMA user_version`.

  * MIGRATIONS is an ordered list of (version, name, fn). Versions are 1, 2, 3, ... with no gaps.
  * Each pending migration runs in its own transaction together with the version bump: if it fails,
    everything it did is rolled back and startup stops (a half-migrated trading database is worse
    than a bot that refuses to start).
  * A database NEWER than this code (user_version too high) is refused, so an old build can't run
    against a schema it doesn't understand.

Migration 1 is idempotent on purpose: databases created before versioning existed carry user_version 0
but any mix of tables/columns, and v1 brings every one of them to the baseline without touching data.

TO CHANGE THE SCHEMA: append a new (N+1, "name", fn) entry below. Never edit or reorder old ones.
"""

import logging

from core.db.connection import get_db_connection


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(conn, table, column, decl):
    """Adds the column unless it exists. Returns True if it was added."""
    if column in _columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


# ---------------------------------------------------------------- v1: baseline (idempotent)

_SIGNAL_COLUMNS = (
    ("symbol", "TEXT"),
    ("score", "INTEGER"),
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
    ("reasoning", "TEXT"),
)


def _v1_baseline(conn):
    conn.execute("""
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
    # SQLite can't add a column with a non-constant default, so updated_at is added bare and backfilled.
    if _add_column(conn, "signals", "updated_at", "DATETIME"):
        conn.execute("UPDATE signals SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP)")
    for column, decl in _SIGNAL_COLUMNS:
        _add_column(conn, "signals", column, decl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ticket ON signals(ticket)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ticket2 ON signals(ticket2)")

    conn.execute("""
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
    conn.execute("""
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
    _add_column(conn, "sweep_runs", "axes_json", "TEXT")
    _add_column(conn, "sweep_results", "params_json", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sweep_results_run ON sweep_results(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sweep_runs_chat ON sweep_runs(chat_id)")


# ---------------------------------------------------------------- v2: indexes for the hot queries

def _v2_status_indexes(conn):
    # get_tracked_signals runs every 30 s filtering on status; the daily-trade count filters on created_at.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at)")


MIGRATIONS = (
    (1, "baseline schema", _v1_baseline),
    (2, "status/created_at indexes", _v2_status_indexes),
)


def _check_sequence(migrations):
    versions = [v for v, _name, _fn in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError(f"migration versions must be 1..N with no gaps or reordering, got {versions}")


_check_sequence(MIGRATIONS)


# ---------------------------------------------------------------- runner

def current_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def latest_version(migrations=None):
    migrations = MIGRATIONS if migrations is None else migrations
    return migrations[-1][0] if migrations else 0


def migrate(conn, migrations=None):
    """Applies every pending migration (each in its own transaction). Returns the final version."""
    migrations = MIGRATIONS if migrations is None else migrations
    current, latest = current_version(conn), latest_version(migrations)
    if current > latest:
        raise RuntimeError(
            f"Database schema is version {current} but this build only knows up to {latest}. "
            f"Refusing to run an older build against a newer database — update the code.")

    for version, name, fn in migrations:
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            fn(conn)
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.commit()
        except Exception:
            conn.rollback()
            logging.exception(f"❌ DB migration {version} ('{name}') failed — rolled back.")
            raise
        logging.info(f"🗄️ DB migration {version} applied: {name}")
    return current_version(conn)


def init_db():
    """Brings the database up to the latest schema. Call once at startup, before anything reads it."""
    with get_db_connection() as conn:
        migrate(conn)