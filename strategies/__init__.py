# strategies/__init__.py
"""
Strategy registry. Every sub-FOLDER of strategies/ (not starting with "_") that exports
`STRATEGY` (a strategies.base.Strategy instance) is one strategy; the folder name is its key
(settings.json "active_strategy", /strategy <key>).

  Add a strategy    -> copy strategies/_template/ to strategies/<name>/ and edit it.
  Remove a strategy -> delete its folder. Its tuned params stay in settings.json and
                       come back if you re-add it.

An empty registry is valid: analysis, /gold, /diagnose, journal and position tracking keep
working; no signals are generated and backtesting is refused.
"""

import importlib
import logging
import pkgutil

from config import ALERT_STATE, TF_BY_NAME
from .base import Strategy, Param, Needs, Params, MarketData, BacktestData

__all__ = [
    "STRATEGIES", "STRATEGY_PRESETS", "get_strategy", "get_params", "get_runtime",
    "reset_runtime", "resolve_param", "find_param", "plan_sweep", "params_overview",
    "Strategy", "Param", "Needs", "Params", "MarketData", "BacktestData",
]

STRATEGIES = {}
_RUNTIME = {}          # strategy key -> mem dict (in-memory only)
_WARNED = set()
_NONE_NAMES = (None, "", "none")


def _check(strat):
    seen = set()
    for prm in strat.params:
        if not isinstance(prm, Param):
            return f"params must be Param instances (got {prm!r})"
        for name in (prm.key, *prm.aliases):
            if name in seen:
                return f"duplicate param name '{name}'"
            seen.add(name)
    for name, _bars in strat.needs.extra:
        if name not in TF_BY_NAME:
            return f"needs.extra: unknown timeframe '{name}' (valid: {', '.join(TF_BY_NAME)})"
    return None


def _initial_value(prm, legacy):
    """Saved value wins (handled by the caller); else the old global setting; else the default."""
    if prm.legacy and prm.legacy in legacy:
        try:
            return prm.parse(str(legacy[prm.legacy]))
        except (ValueError, TypeError):
            pass
    return prm.default


def _discover():
    store = ALERT_STATE.setdefault("strategy_params", {})
    legacy = ALERT_STATE.get("_legacy", {})
    for info in pkgutil.iter_modules(__path__):
        name = info.name
        if name.startswith("_") or name == "base":
            continue
        if not info.ispkg:
            logging.warning(f"⚠️ strategies/{name}.py is a legacy single-file strategy — ignored. "
                            f"Move it into strategies/{name}/ (see strategies/_template).")
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
        except Exception:
            logging.exception(f"❌ Strategy '{name}' failed to import — skipped.")
            continue
        strat = getattr(mod, "STRATEGY", None)
        if not isinstance(strat, Strategy):
            logging.error(f"❌ Strategy '{name}' does not export STRATEGY (a Strategy instance) — skipped.")
            continue
        problem = _check(strat)
        if problem:
            logging.error(f"❌ Strategy '{name}' rejected: {problem}")
            continue

        strat.key = name
        mine = store.setdefault(name, {})
        for prm in strat.params:
            if prm.key not in mine:
                mine[prm.key] = _initial_value(prm, legacy)
        STRATEGIES[name] = strat


_discover()
STRATEGIES = dict(sorted(STRATEGIES.items()))
STRATEGY_PRESETS = {k: s.label for k, s in STRATEGIES.items()}     # {key: description}

if not STRATEGIES:
    logging.warning("⚠️ No strategies installed in strategies/ — signal generation and backtesting are disabled.")


def get_strategy(name):
    """Strategy instance, or None for 'none' / unknown / not installed.
    Deliberately NO silent fallback: a removed strategy must stop trading, not quietly become another one."""
    if isinstance(name, str):
        name = name.strip().lower()
    if name in _NONE_NAMES:
        return None
    strat = STRATEGIES.get(name)
    if strat is None and name not in _WARNED:
        _WARNED.add(name)
        logging.warning(f"⚠️ Active strategy '{name}' is not installed — no signals will be generated.")
    return strat


def get_params(key):
    """Fresh Params snapshot of a strategy's current settings."""
    return Params(ALERT_STATE.get("strategy_params", {}).get(key, {}))


def get_runtime(key):
    """The strategy's in-memory scratch dict (dedup keys, watch debounce). Cleared by reset_runtime()."""
    return _RUNTIME.setdefault(key, {})


def reset_runtime():
    """Switching strategy/timeframe: clear every strategy's dedup + watch memory."""
    _RUNTIME.clear()


# ---------------------------------------------------------------- param lookup

def _match(strat, token):
    t = str(token).lower().strip()
    for prm in strat.params:
        if t in (prm.key, *prm.aliases, f"{strat.key}.{prm.key}"):
            return prm
    return None


def find_param(strategy_key, token):
    """Param of ONE strategy by key/alias (or 'strategy.key'); None if it has no such param."""
    strat = STRATEGIES.get(strategy_key)
    return _match(strat, token) if strat else None


def resolve_param(token):
    """'adx' or 'trend_breakout.adx' -> list of (strategy_key, Param) across all strategies."""
    scope, _, name = str(token).lower().strip().rpartition(".")
    hits = []
    for skey, strat in STRATEGIES.items():
        if scope and skey != scope:
            continue
        for prm in strat.params:
            if name == prm.key or name in prm.aliases:
                hits.append((skey, prm))
    return hits


# ---------------------------------------------------------------- optimize planning

def _thin(vals, k):
    """Keep ≤k evenly spaced values (always including both ends)."""
    vals = list(vals)
    if len(vals) <= k:
        return vals
    if k <= 1:
        return vals[:1]
    idx = sorted({round(i * (len(vals) - 1) / (k - 1)) for i in range(k)})
    return [vals[i] for i in idx]


def plan_sweep(keys, axes, max_combos=150, max_axes=3):
    """
    Builds an /optimize grid from each strategy's own sweepable params.
    keys: strategies to sweep. axes: requested axis names (param key/alias); empty -> each
    strategy's first two sweepable params.
    Returns (plan, used_default_axes, error). plan = [(strategy_key, {param_key: [values]})].
    """
    keys = [k for k in keys if k in STRATEGIES]
    if not keys:
        return None, False, "No strategy to sweep."
    axes = [a.lower() for a in axes]
    used_default = not axes
    budget = max(1, max_combos // len(keys))
    plan, matched = [], set()

    for key in keys:
        strat = STRATEGIES[key]
        sweepable = [prm for prm in strat.params if prm.sweep]
        if used_default:
            chosen = sweepable[:2]
        else:
            chosen = []
            for a in axes:
                prm = _match(strat, a)
                if prm is not None and prm.sweep:
                    matched.add(a)
                    if prm not in chosen:
                        chosen.append(prm)
        if len(chosen) > max_axes:
            return None, False, f"Combine at most {max_axes} axes at once to keep the grid manageable."

        k = max((len(prm.sweep) for prm in chosen), default=1)
        while k > 1 and _grid_size(chosen, k) > budget:
            k -= 1
        plan.append((key, {prm.key: _thin(prm.sweep, k) for prm in chosen}))

    unknown = [a for a in axes if a not in matched]
    if unknown:
        valid = sorted({prm.key for key in keys for prm in STRATEGIES[key].params if prm.sweep})
        return None, False, (f"Unknown or non-sweepable axis: `{', '.join(unknown)}`. "
                             f"Sweepable: `{'`, `'.join(valid) or 'none'}`.")
    return plan, used_default, None


def _grid_size(chosen, k):
    n = 1
    for prm in chosen:
        n *= min(len(prm.sweep), k)
    return n


def params_overview(only=None):
    """Telegram-Markdown listing of strategy parameters (used by /set)."""
    if not STRATEGIES:
        return "📭 No strategies installed."
    out = ["🧩 **STRATEGY PARAMETERS**\n"]
    for key, strat in STRATEGIES.items():
        if only and key != only:
            continue
        out.append(f"`{key}` — {strat.label.replace('_', ' ')}")
        vals = ALERT_STATE.get("strategy_params", {}).get(key, {})
        if not strat.params:
            out.append("  _(no parameters)_")
        for prm in strat.params:
            tag = " 🔁" if prm.sweep else ""
            out.append(f"• `{key}.{prm.key}` = `{vals.get(prm.key, prm.default)}`{tag} — {prm.help.replace('_', ' ')}")
        out.append("")
    out.append("🔁 = sweepable in `/optimize`\n**Usage:** `/set <strategy>.<param> <value>`")
    return "\n".join(out)[:3900]