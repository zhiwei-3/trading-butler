# core/guards/__init__.py
"""
Guard pipeline. A guard is a small async check that returns None (all clear) or a reason string (blocked).

  @guard("spread", scope="scan", order=20)
  async def check(ctx): ...

  scope="scan"    runs BEFORE analysis; any block pauses the whole scan (no analysis, no heads-up pings).
  scope="signal"  runs AFTER analysis (ctx.analysis is set); a block suppresses trade signals only.

Every guard in a scope is evaluated (no short-circuit) so /status can show exactly what is blocking.
A guard that crashes FAILS CLOSED — a broken safety check must not silently stop protecting the account —
and logs the traceback. A guard that merely lacks data (feed down, no tick) should return None itself.
Add a guard = add a file in this folder.
"""

import importlib
import logging
import pkgutil
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class GuardContext:
    symbol: str
    analysis: Optional[dict] = None          # set for scope="signal"


@dataclass(frozen=True)
class Block:
    name: str
    reason: str


@dataclass(frozen=True)
class Guard:
    name: str
    fn: Callable[[GuardContext], Any]        # async -> Optional[str]
    scope: str
    order: int


GUARDS = {}
_STATE = {}                                  # name -> (reason | None, monotonic time of the last run)
_SCOPES = ("scan", "signal")


def _now():
    return time.monotonic()


def guard(name, *, scope="scan", order=50):
    def deco(fn):
        if scope not in _SCOPES:
            raise ValueError(f"guard '{name}': scope must be one of {_SCOPES}")
        if name in GUARDS:
            raise ValueError(f"Duplicate guard '{name}'")
        GUARDS[name] = Guard(name, fn, scope, order)
        return fn
    return deco


async def run_guards(scope, ctx):
    """Runs every guard of `scope` in order. Returns the list of Blocks (empty = clear to proceed)."""
    blocks = []
    for g in sorted((g for g in GUARDS.values() if g.scope == scope), key=lambda g: (g.order, g.name)):
        try:
            reason = await g.fn(ctx)
        except Exception:
            logging.exception(f"❌ Guard '{g.name}' crashed — failing closed.")
            reason = "guard crashed (see log) — failing closed"
        _STATE[g.name] = (reason, _now())
        if reason:
            blocks.append(Block(g.name, reason))
    return blocks


def active_blocks(max_age=300):
    """{guard: reason} for guards whose latest verdict (within max_age seconds) was a block. Used by /status."""
    now = _now()
    return {name: reason for name, (reason, at) in _STATE.items() if reason and now - at <= max_age}


for _m in pkgutil.iter_modules(__path__):
    if not _m.name.startswith("_"):
        # Fatal on purpose: a silently missing safety guard is worse than a bot that refuses to start.
        importlib.import_module(f"{__name__}.{_m.name}")