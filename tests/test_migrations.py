# tests/test_migrations.py
import sqlite3

import pytest

from core.db import migrations


def _conn():
    return sqlite3.connect(":memory:")


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def test_a_fresh_database_reaches_the_latest_version_with_every_table_column_and_index():
    conn = _conn()
    assert migrations.migrate(conn) == migrations.latest_version()
    assert {"ticket", "ticket2", "be_moved2", "zone_low", "reasoning", "updated_at", "dual_entry"} <= _cols(conn, "signals")
    assert "axes_json" in _cols(conn, "sweep_runs") and "params_json" in _cols(conn, "sweep_results")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_signals_ticket", "idx_signals_status", "idx_signals_created", "idx_sweep_results_run"} <= names


def test_a_legacy_unversioned_database_is_upgraded_and_keeps_its_rows():
    conn = _conn()
    conn.execute("""CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, direction TEXT,
                    entry_price REAL, sl_price REAL, tp1_price REAL, tp2_price REAL, status TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("INSERT INTO signals (timestamp, direction, entry_price, sl_price, tp1_price, tp2_price, "
                 "status, created_at) VALUES ('t', 'BUY', 2000, 1990, 2015, 2030, 'HIT_SL', '2026-01-01 10:00:00')")
    conn.commit()
    assert _version(conn) == 0

    migrations.migrate(conn)

    row = conn.execute("SELECT status, updated_at, exec_mode, dual_entry, be_moved2 FROM signals").fetchone()
    assert row == ("HIT_SL", "2026-01-01 10:00:00", "NONE", 0, 0)       # updated_at backfilled, defaults applied
    assert _version(conn) == migrations.latest_version()


def test_a_current_schema_that_was_never_stamped_is_adopted_without_data_changes():
    """This is today's production database: full schema, user_version 0."""
    conn = _conn()
    migrations.migrate(conn)
    conn.execute("INSERT INTO signals (timestamp, symbol, direction, status) VALUES ('t', 'XAUUSD', 'BUY', 'PENDING')")
    conn.commit()
    conn.execute("PRAGMA user_version = 0")

    migrations.migrate(conn)

    assert _version(conn) == migrations.latest_version()
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1


def test_applied_migrations_never_run_again():
    runs = []
    migs = ((1, "a", lambda c: runs.append(1)), (2, "b", lambda c: runs.append(2)))
    conn = _conn()
    migrations.migrate(conn, migs)
    migrations.migrate(conn, migs)
    assert runs == [1, 2] and _version(conn) == 2


def test_a_failing_migration_rolls_back_completely_and_keeps_the_old_version():
    def ok(c):
        c.execute("CREATE TABLE a (x)")

    def boom(c):
        c.execute("CREATE TABLE b (x)")
        raise RuntimeError("bad migration")

    conn = _conn()
    with pytest.raises(RuntimeError):
        migrations.migrate(conn, ((1, "ok", ok), (2, "boom", boom)))

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" in tables and "b" not in tables          # migration 2 left nothing behind
    assert _version(conn) == 1                          # and the version still says 1


def test_a_database_newer_than_the_code_is_refused():
    conn = _conn()
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="newer database|only knows up to"):
        migrations.migrate(conn)


def test_the_real_migration_list_is_sequential():
    assert [v for v, _n, _f in migrations.MIGRATIONS] == list(range(1, len(migrations.MIGRATIONS) + 1))
    with pytest.raises(ValueError):
        migrations._check_sequence(((1, "a", None), (3, "c", None)))