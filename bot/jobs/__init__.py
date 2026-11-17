"""Every non-underscore module in this package is imported on startup; its @job decorators register it
(see bot/registry.py). Add a job = add a file.

Unlike bot/commands, a job that fails to import is FATAL: a silently missing outcome tracker or
watchdog would leave live trades unmonitored, so startup should stop instead."""

import importlib
import pkgutil

for _m in pkgutil.iter_modules(__path__):
    if not _m.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_m.name}")