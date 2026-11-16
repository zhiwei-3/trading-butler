"""Every non-underscore module in this package is imported on startup; its @command / @callback
decorators register it (see bot/registry.py). Add a command = add a file or a decorated function."""

import importlib
import logging
import pkgutil

for _m in pkgutil.iter_modules(__path__):
    if _m.name.startswith("_"):
        continue
    try:
        importlib.import_module(f"{__name__}.{_m.name}")
    except Exception:
        logging.exception(f"❌ Command module '{_m.name}' failed to import — its commands are unavailable.")