# strategies/base.py
"""
The contract every strategy package implements.

PURITY: a strategy may import only `config`, `utils.*`, numpy and pandas (plus this module).
No MT5, no database, no Telegram, no charts, no `core.*`, no `bot.*`.
The core fetches data and hands it in through the hooks below.
"""

from dataclasses import dataclass, field
from typing import Any, Callable


class Params(dict):
    """Dict with attribute access: p.lookback == p["lookback"]."""
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k) from None


@dataclass(frozen=True)
class Param:
    """One strategy-owned setting. Persisted in settings.json under strategy_params[<key>]."""
    key: str
    default: Any
    parse: Callable[[str], Any]      # str -> value, raises ValueError on bad input
    aliases: tuple = ()
    help: str = ""
    sweep: tuple = ()                # candidate values for /optimize (empty = not sweepable)
    legacy: str = ""                 # old global settings.json key to migrate the saved value from


@dataclass(frozen=True)
class Needs:
    """What the strategy asks the core to provide."""
    entry_bars: int = 200            # live entry-TF candles to fetch
    extra: tuple = ()                # ((tf_name, live_bars), ...)  e.g. (("H1", 400),)  names: see config.TF_BY_NAME
    vol_filter: bool = False         # backtester computes vol_filter_ok per bar
    standard_analysis: bool = True   # backtester builds swings/FVG/OB/S&R "full" dict per bar (slow; skip if unused)


@dataclass
class MarketData:
    """Live data bundle handed to build_live_context(). Entry frame already has EMA_20/50, RSI, ATR."""
    symbol: str
    entry: Any
    trend: Any
    macro: Any
    extra: dict = field(default_factory=dict)      # {tf_name: DataFrame} for needs.extra
    broker_offset_s: int = 0                       # add to a UTC epoch to get broker-server epoch


@dataclass
class BacktestData:
    """Backtest data bundle handed to prepare_backtest(). Frames carry precomputed indicators."""
    symbol: str
    entry: Any
    trend: Any
    macro: Any
    extra: dict
    entry_minutes: int
    broker_offset_s: int
    memo: dict = field(default_factory=dict)       # shared across all runs of one /optimize sweep: cache heavy precompute here


class Strategy:
    """Subclass, override what you need, export one instance as STRATEGY in the package __init__."""
    key = ""                 # set by the registry (= folder name)
    label = "(unnamed strategy)"
    needs = Needs()
    params = ()              # tuple[Param]

    def is_dual(self, score, p):
        """True -> 2 legs (TP1 full close + TP2/BE runner)."""
        return False

    # ---------------- shared hooks
    def valid_params(self, p):
        """Cross-field sanity (e.g. rsi_buy < rsi_sell). Used by /set and skips invalid /optimize combos."""
        return True

    def summary(self, p):
        """One-liner for /menu and /status."""
        return " · ".join(f"{k}={v}" for k, v in list(p.items())[:5]) or "—"

    def watch(self, a, p, mem):
        """Early 'setup forming' heads-ups. Return a list of Telegram-Markdown strings.
        The master on/off switch (/watchlist) is global; the debounce state lives in `mem`."""
        return []

    def diagnose(self, a, p):
        """Extra lines for /diagnose (Telegram Markdown, may be empty)."""
        return ""

    # ---------------- live
    def build_live_context(self, md, p):
        """CONNECTOR. md: MarketData. Return any object; it arrives as a['ctx'] in evaluate()."""
        return None

    def evaluate(self, a, p, mem):
        """a: analysis dict (+ a['ctx']). p: Params. mem: this strategy's runtime dict (dedup etc., not persisted).
        Return a list of utils.common.signal_spec(...) dicts."""
        return []

    # ---------------- backtest
    def init_state(self, p):
        """Extra keys merged into the per-run backtest `state` dict (dedup memory etc.)."""
        return {}

    def prepare_backtest(self, bt, p):
        """CONNECTOR, once per run. bt: BacktestData. Return anything; it is passed to bar_context()."""
        return None

    def bar_context(self, prep, i, p, state):
        """CONNECTOR, once per entry bar i. Return the ctx for decide(), or None to SKIP this bar cheaply."""
        return {}

    def decide(self, c):
        """c: {close_price, rsi_val, atr_val, entry/trend/macro_bullish, full, ctx, state, p,
               min_rrr, min_confluence_score, sl_mult, tp1_mult, tp2_mult, structure, near_zone}.
        Return a trade dict (direction, entry, score, breakdown, sl/tp1/tp2_price, sl/tp1/tp2_dist, ...) or None."""
        return None
