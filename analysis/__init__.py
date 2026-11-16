# analysis/__init__.py
"""Shared market-analysis pipeline. Pure: no MT5, DB, Telegram or charts.
Live (core.evaluator) and backtest (core.backtester) both call it, so a feature can never drift between them."""

from .indicators import add_indicators
from .features import FEATURES, ALL, View, compute, feature, default_config

__all__ = ["add_indicators", "FEATURES", "ALL", "View", "compute", "feature", "default_config"]