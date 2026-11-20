# core/db/stats.py
"""Derived numbers: daily R performance (circuit breaker), SL cooldown, signal counts, live fills today."""

import logging
from datetime import datetime, timezone, timedelta, time as dtime

from config import ALERT_STATE
from core.db.connection import get_db_connection


def local_day_start_utc_str():
    """Start of the current LOCAL day (PC timezone), as a UTC 'YYYY-MM-DD HH:MM:SS' string.
    The DB stores UTC, so local-day filters compare against this converted boundary."""
    local_midnight = datetime.combine(datetime.now().astimezone().date(), dtime.min).astimezone()
    return local_midnight.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def count_live_trades_today():
    """Live fills (tickets) opened since local midnight (PC timezone)."""
    today_start = local_day_start_utc_str()
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


def get_signal_stats():
    """Retrieves current signal outcome statistics: {status: count}."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) AS count FROM signals GROUP BY status")
        return {row["status"]: row["count"] for row in cursor.fetchall()}


def get_daily_performance_stats():
    """Today's CLOSED trade performance (local day, PC timezone). R is computed from each signal's real levels,
    using the same model as the backtester (dual: TP1/2 leg-averages; BE runner = tp1_r/2)."""
    today_start = local_day_start_utc_str()
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


def get_sl_cooldown_remaining_s():
    """Seconds left in the post-stop-loss cooldown (0 if none / disabled).
    Derived from the newest HIT_SL row, so it survives restarts with no extra state."""
    if not ALERT_STATE.get("sl_cooldown_enabled", True):
        return 0.0
    try:
        mins = float(ALERT_STATE.get("sl_cooldown_minutes", 30))
    except (TypeError, ValueError):
        return 0.0
    if mins <= 0:
        return 0.0
    try:
        with get_db_connection() as conn:
            row = conn.execute("SELECT MAX(updated_at) AS t FROM signals WHERE status = 'HIT_SL'").fetchone()
    except Exception as e:
        logging.warning(f"Cooldown lookup failed: {e}")
        return 0.0
    if not row or not row["t"]:
        return 0.0
    try:
        last = datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    remaining = (last + timedelta(minutes=mins) - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, remaining)