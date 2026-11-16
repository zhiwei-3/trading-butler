# analysis/features.py
"""
Feature registry. A feature is a function of a `View` (everything knowable at ONE bar).
  * Computed lazily + memoised per View, so dependencies (sweeps -> swings) are free.
  * scope="macro" features depend only on the higher-timeframe frame; the backtester passes a
    `macro_store` so they are recomputed only when a new macro bar closes.
Strategies declare `needs.features = ("fvg", "near_zone", ...)`; the core computes exactly those.
Add a feature = write one decorated function here.
"""

from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from config import ALERT_STATE
from .smc import (
    find_swing_points, detect_market_structure, detect_liquidity_sweeps, detect_fvg,
    detect_order_block, find_unmitigated_fvgs, fvg_snapshot_from_gaps,
)
from .indicators import (
    detect_candlestick_pattern, detect_divergence, find_sr_zones, nearest_sr_zone, vol_filter_ok,
)

_CFG_KEYS = ("fractal_window", "sr_lookback", "sr_cluster_pct", "sr_min_touches",
             "sr_max_distance_pct", "atr_multiplier", "volume_multiplier")


def default_config():
    """Analysis settings snapshot (from the live settings)."""
    return {k: ALERT_STATE[k] for k in _CFG_KEYS}


def _resolve(x):
    return x() if callable(x) else x


class View:
    """Everything a feature may read at the current bar.
    entry / macro may be DataFrames or zero-arg callables (lazy slicing in backtests)."""

    def __init__(self, entry, price, atr, cfg, macro=None, macro_store=None, macro_key=None):
        self._entry, self._macro = entry, macro
        self._entry_df = self._macro_df = None
        self.price, self.atr, self.cfg = float(price), float(atr), cfg
        self._macro_store, self._macro_key = macro_store, macro_key
        self._memo = {}

    @property
    def entry(self):
        if self._entry_df is None:
            self._entry_df = _resolve(self._entry)
        return self._entry_df

    @property
    def macro(self):
        if self._macro_df is None and self._macro is not None:
            self._macro_df = _resolve(self._macro)
        return self._macro_df

    def get(self, name):
        if name in self._memo:
            return self._memo[name]
        feat = FEATURES[name]
        store = self._macro_store
        if feat.scope == "macro" and store is not None:
            if store.get("key") != self._macro_key:           # a new macro bar closed -> drop the cache
                store["key"], store["values"] = self._macro_key, {}
            if name not in store["values"]:
                store["values"][name] = feat.fn(self)
            val = store["values"][name]
        else:
            val = feat.fn(self)
        self._memo[name] = val
        return val


@dataclass(frozen=True)
class Feature:
    name: str
    fn: Callable[[View], Any]
    scope: str = "bar"          # "bar" | "macro"
    public: bool = True         # public features are part of ALL


FEATURES = {}


def feature(name, scope="bar", public=True):
    def deco(fn):
        if name in FEATURES:
            raise ValueError(f"Duplicate analysis feature '{name}'")
        FEATURES[name] = Feature(name, fn, scope, public)
        return fn
    return deco


def compute(names, view):
    """{name: value} for the requested features."""
    out = {}
    for n in names:
        if n not in FEATURES:
            raise KeyError(f"Unknown analysis feature '{n}' (available: {', '.join(FEATURES)})")
        out[n] = view.get(n)
    return out


# ---------------------------------------------------------------- entry-timeframe features

@feature("swings", public=False)
def _swings(v):
    w = v.cfg["fractal_window"]
    return find_swing_points(v.entry, w, w)


@feature("sweeps")
def _sweeps(v):
    highs, lows = v.get("swings")
    return detect_liquidity_sweeps(v.entry, highs, lows)


@feature("fvg")
def _fvg(v):
    return detect_fvg(v.entry)


@feature("order_block")
def _order_block(v):
    return detect_order_block(v.entry)


@feature("structure")
def _structure(v):
    return detect_market_structure(v.entry)


@feature("macd_bias")
def _macd_bias(v):
    if "MACD_BIAS" not in v.entry.columns:
        return None
    val = v.entry["MACD_BIAS"].iloc[-1]
    return None if pd.isna(val) else val


@feature("candle_pattern")
def _candle_pattern(v):
    return detect_candlestick_pattern(v.entry)


@feature("divergence")
def _divergence(v):
    return detect_divergence(v.entry)


@feature("vol_filter_ok")
def _vol_ok(v):
    return vol_filter_ok(v.entry.iloc[-1], v.cfg["atr_multiplier"], v.cfg["volume_multiplier"])


# ---------------------------------------------------------------- macro-timeframe features

@feature("sr_zones", scope="macro", public=False)
def _sr_zones(v):
    if v.macro is None:
        return []
    c = v.cfg
    return find_sr_zones(v.macro, lookback=c["sr_lookback"], cluster_pct=c["sr_cluster_pct"],
                         min_touches=c["sr_min_touches"])


@feature("macro_gaps", scope="macro", public=False)
def _macro_gaps(v):
    if v.macro is None:
        return []
    return find_unmitigated_fvgs(v.macro, lookback=v.cfg["sr_lookback"])


# ---------------------------------------------------------------- price-dependent views of the macro features

@feature("near_zone")
def _near_zone(v):
    return nearest_sr_zone(v.get("sr_zones"), v.price, max_distance_pct=v.cfg["sr_max_distance_pct"])


@feature("macro_fvg")
def _macro_fvg(v):
    return fvg_snapshot_from_gaps(v.get("macro_gaps"), v.price)


ALL = tuple(n for n, f in FEATURES.items() if f.public)    # what /gold and /diagnose ask for