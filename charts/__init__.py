# charts/__init__.py
"""Chart rendering (in-memory PNG via matplotlib). Pure with respect to the app: imports config,
numpy, pandas and matplotlib only — never core/, bot/, MT5 or Telegram. Callers pass whatever the
chart needs, including the broker UTC offset (server_offset_s)."""

import matplotlib
matplotlib.use("Agg")          # headless backend; must run before anything imports pyplot

from .theme import CHART_LOCK
from .snapshot import generate_chart_snapshot
from .alert import generate_signal_chart
from .outcome import generate_outcome_chart
from .journal import generate_journal_chart
from .equity import generate_equity_chart

__all__ = [
    "CHART_LOCK", "generate_chart_snapshot", "generate_signal_chart",
    "generate_outcome_chart", "generate_journal_chart", "generate_equity_chart",
]