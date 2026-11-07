# strategies/__init__.py
"""
Auto-discovering strategy registry — PURE RULES ONLY.

Every non-underscore .py file in this folder is ONE strategy; the filename is its key
(what goes in settings.json "active_strategy" and /strategy <key>).

  Add a strategy    -> drop a file in this folder.
  Remove a strategy -> delete its file.

Purity rule: a strategy may import only `config`, `utils.*`, numpy and pandas.
No MT5, no database, no Telegram, no charts, no `core.*`, no `bot.*`.

A strategy module must define:
    LABEL              str   - description shown in /strategy, the menu and /optimize
    is_dual(score)     bool  - True if it opens 2 legs (TP1 + TP2/BE runner)
    evaluate(a)        live  -> (signal_specs, watches)   [utils.common.signal_spec dicts]
    decide(ctx)        backtest -> trade dict or None
Optional hooks (still pure — the evaluator does the fetching and passes data in):
    LIVE_ENTRY_BARS    int   - entry-TF candles analyze_market should fetch (default 200)
    build_live_context(df_entry, df_h1, broker_offset_s) -> dict placed in analysis["tb"]
"""

import importlib
import logging
import pkgutil

_REQUIRED = ("LABEL", "is_dual", "evaluate", "decide")
_PREFERRED_DEFAULT = "smc_confluence"

STRATEGIES = {}

for _info in pkgutil.iter_modules(__path__):
    if _info.name.startswith("_"):
        continue
    try:
        _mod = importlib.import_module(f"{__name__}.{_info.name}")
    except Exception:
        logging.exception(f"❌ Strategy '{_info.name}' failed to import — skipped.")
        continue
    _missing = [attr for attr in _REQUIRED if not hasattr(_mod, attr)]
    if _missing:
        logging.error(f"❌ Strategy '{_info.name}' is missing {_missing} — skipped.")
        continue
    STRATEGIES[_info.name] = _mod

if not STRATEGIES:
    raise RuntimeError("No strategies found in strategies/ — at least one is required.")

_order = sorted(STRATEGIES, key=lambda k: (k != _PREFERRED_DEFAULT, k))
STRATEGIES = {k: STRATEGIES[k] for k in _order}
DEFAULT_STRATEGY = _PREFERRED_DEFAULT if _PREFERRED_DEFAULT in STRATEGIES else _order[0]

# {key: description}
STRATEGY_PRESETS = {k: m.LABEL for k, m in STRATEGIES.items()}


def get_strategy(name):
    """Unknown/removed names fall back to the default strategy (with a warning)."""
    mod = STRATEGIES.get(name)
    if mod is None:
        logging.warning(f"⚠️ Strategy '{name}' not found — falling back to '{DEFAULT_STRATEGY}'.")
        mod = STRATEGIES[DEFAULT_STRATEGY]
    return mod