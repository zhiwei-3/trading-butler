# core/db/__init__.py
"""SQLite persistence. The only package that imports sqlite3 (tests/test_architecture.py enforces this).

    from core.db import init_db, log_signal_to_db, get_tracked_signals, ...

Schema changes are versioned migrations in core/db/migrations.py.
"""

from core.db.connection import get_db_connection, utc_now_str
from core.db.migrations import init_db, migrate, current_version, latest_version
from core.db.signals import (
    log_signal_to_db, attach_trade_execution, mark_leg2_be_moved, set_signal_status,
    get_tracked_signals, update_signal_statuses, get_open_dry_run_signals, get_active_live_signals,
    get_open_executed_signals, get_dry_run_journal_signals, get_live_signal_links,
    get_recent_trades_for_chart,
)
from core.db.stats import (
    count_live_trades_today, get_signal_stats, get_daily_performance_stats, get_sl_cooldown_remaining_s,
)
from core.db.sweeps import (
    log_sweep_run, get_sweep_results, get_sweep_run, get_latest_sweep_run,
    count_sweep_runs, get_sweep_history, get_sweep_history_ranked,
)

__all__ = [
    "get_db_connection", "utc_now_str", "init_db", "migrate", "current_version", "latest_version",
    "log_signal_to_db", "attach_trade_execution", "mark_leg2_be_moved", "set_signal_status",
    "get_tracked_signals", "update_signal_statuses", "get_open_dry_run_signals", "get_active_live_signals",
    "get_open_executed_signals", "get_dry_run_journal_signals", "get_live_signal_links",
    "get_recent_trades_for_chart",
    "count_live_trades_today", "get_signal_stats", "get_daily_performance_stats", "get_sl_cooldown_remaining_s",
    "log_sweep_run", "get_sweep_results", "get_sweep_run", "get_latest_sweep_run",
    "count_sweep_runs", "get_sweep_history", "get_sweep_history_ranked",
]