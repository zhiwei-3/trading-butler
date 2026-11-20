# core/db/sweeps.py
"""/optimize sweep history."""

import json
import logging

from core.db.connection import get_db_connection

_RANK_COLUMNS = ("net_r", "total_trades", "avg_r", "max_drawdown_r", "win_rate", "calmar",
                 "wins", "losses", "strategy")


def log_sweep_run(chat_id, symbol, timeframe_mode, days, rank_mode, axes, min_sample, elapsed_seconds, rows):
    """Persists a completed /optimize sweep run. `axes` = swept axis names; each row carries
    {"strategy", "params": {...}, metrics...}. Legacy columns are still filled where they apply."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO sweep_runs
                (chat_id, symbol, timeframe_mode, days, rank_mode,
                 swept_strategies, swept_sl, swept_rsi, swept_rrr, swept_score,
                 min_sample, elapsed_seconds, axes_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                chat_id, symbol, timeframe_mode, days, rank_mode,
                int("strategies" in axes), int("sl_mult" in axes), int("rsi_buy" in axes or "rsi_sell" in axes),
                int("min_rrr" in axes), int("min_score" in axes),
                min_sample, elapsed_seconds, json.dumps(list(axes)),
            ))
            run_id = cursor.lastrowid

            for i, row in enumerate(rows, start=1):
                p = row.get("params") or {}
                cursor.execute("""
                    INSERT INTO sweep_results
                    (run_id, rank, strategy, min_rrr, min_confluence_score, sl_atr_mult,
                     rsi_buy, rsi_sell, total_trades, wins, losses, open_trades,
                     win_rate, avg_r, net_r, max_drawdown_r, calmar, params_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    run_id, i, row.get("strategy"), p.get("min_rrr"), p.get("min_score"), p.get("sl_mult"),
                    p.get("rsi_buy"), p.get("rsi_sell"),
                    row.get("total_trades", 0), row.get("wins", 0), row.get("losses", 0), row.get("open", 0),
                    row.get("win_rate"), row.get("avg_r"), row.get("net_r"), row.get("max_drawdown_r"),
                    row.get("calmar"), json.dumps(p),
                ))
            conn.commit()
            return run_id
    except Exception as e:
        logging.error(f"⚠️ Failed to log sweep run to DB: {e}")
        return None


def _legacy_params(d):
    """Pre-modular sweep rows only have fixed columns; map them onto today's param names."""
    m = {"min_rrr": d.get("min_rrr"), "min_score": d.get("min_confluence_score"),
         "sl_mult": d.get("sl_atr_mult"), "rsi_buy": d.get("rsi_buy"), "rsi_sell": d.get("rsi_sell")}
    return {k: v for k, v in m.items() if v is not None}


def get_sweep_results(run_id, limit=None):
    query = "SELECT * FROM sweep_results WHERE run_id = ? ORDER BY rank ASC"
    params = [run_id]
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    with get_db_connection() as conn:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    for d in rows:
        try:
            d["params"] = json.loads(d["params_json"]) if d.get("params_json") else _legacy_params(d)
        except ValueError:
            d["params"] = _legacy_params(d)
    return rows


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
    if sort_column not in _RANK_COLUMNS:
        raise ValueError(f"cannot rank sweeps by {sort_column!r} (allowed: {', '.join(_RANK_COLUMNS)})")
    if str(sort_dir).upper() not in ("ASC", "DESC"):
        raise ValueError(f"sort_dir must be ASC or DESC, got {sort_dir!r}")
    with get_db_connection() as conn:
        query = f"""
            SELECT sr.*, res.strategy AS top_strategy, res.net_r AS top_net_r, res.avg_r AS top_avg_r,
                   res.win_rate AS top_win_rate, res.wins AS top_wins, res.losses AS top_losses,
                   res.total_trades AS top_trades, res.max_drawdown_r AS top_dd, res.calmar AS top_calmar
            FROM sweep_runs sr
            JOIN sweep_results res ON res.run_id = sr.id AND res.rank = 1
            WHERE sr.chat_id = ?
            ORDER BY res.{sort_column} {str(sort_dir).upper()}
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(query, (chat_id, limit, offset)).fetchall()
        return [dict(r) for r in rows]