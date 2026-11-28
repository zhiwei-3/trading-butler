# core/db/signals.py
"""Signal rows: logging, execution links, status transitions and the lists the jobs/commands read."""

import logging
from datetime import datetime, timezone

from core.db.connection import get_db_connection, utc_now_str


def log_signal_to_db(symbol, direction, entry_price, sl_price, tp1_price, tp2_price, score,
                     dual_entry=False, near_zone=None, reasoning=None, strategy=None):
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
                                     score, status, dual_entry, zone_low, zone_high, zone_type, reasoning, strategy)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?)
            ''', (timestamp, symbol, direction, entry_price, sl_price, tp1_price, tp2_price, score,
                  int(bool(dual_entry)), zone_low, zone_high, zone_type, reasoning, strategy))
            conn.commit()
            return cursor.lastrowid
    except Exception as e:
        logging.error(f"⚠️ Failed to log signal to DB: {e}")
        return None


def attach_trade_execution(signal_id, leg, ticket, lots, exec_mode, fill_price=None):
    """Links a live/dry MT5 fill back onto its originating signal row."""
    if not signal_id:
        return
    LEG_COLUMNS = {
        1: ("ticket", "lots", "exec_mode", "fill_price"),
        2: ("ticket2", "lots2", "exec_mode2", "fill_price2"),
    }
    cols = " = ?, ".join(LEG_COLUMNS[leg]) + " = ?"
    try:
        with get_db_connection() as conn:
            conn.execute(
                f"UPDATE signals SET {cols}, updated_at = ? WHERE id = ?",
                (ticket, lots, exec_mode, fill_price, utc_now_str(), signal_id)
            )
            conn.commit()
    except Exception as e:
        logging.error(f"⚠️ Failed to attach leg {leg} execution to signal {signal_id}: {e}")


def mark_leg2_be_moved(signal_id):
    if not signal_id:
        return
    try:
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE signals SET be_moved2 = 1, updated_at = ? WHERE id = ?",
                (utc_now_str(), signal_id)
            )
            conn.commit()
    except Exception as e:
        logging.error(f"⚠️ Failed to mark be_moved2 for signal {signal_id}: {e}")


def set_signal_status(signal_id, status):
    with get_db_connection() as conn:
        conn.execute("UPDATE signals SET status = ?, updated_at = ? WHERE id = ?",
                     (status, utc_now_str(), signal_id))
        conn.commit()


def get_tracked_signals():
    """Signals whose outcome is still being tracked (PENDING, or dual with a running runner)."""
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, symbol, status, direction, entry_price, sl_price, tp1_price, tp2_price, "
            "dual_entry, ticket, exec_mode, lots, fill_price, ticket2, exec_mode2, lots2, fill_price2, "
            "be_moved2, zone_low, zone_high, zone_type "
            "FROM signals WHERE status IN ('PENDING', 'HIT_TP1')"
        ).fetchall()
        return [dict(r) for r in rows]


def update_signal_statuses(writes):
    """writes: [(new_status, updated_at, signal_id), ...]"""
    if not writes:
        return
    with get_db_connection() as conn:
        conn.executemany("UPDATE signals SET status = ?, updated_at = ? WHERE id = ?", writes)
        conn.commit()


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