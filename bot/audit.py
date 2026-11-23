# bot/audit.py
"""User-activity auditing for Telegram handlers."""

import functools
import logging
import time
from contextvars import ContextVar

from config import USER_ID
from utils.logkit import audit

in_batch = ContextVar("tb_in_batch", default=False)     # set by bot/batch.py while it replays lines


def actor(update):
    """{'uid','user','chat'} for any Update-like object (works through batch proxies)."""
    u = getattr(update, "effective_user", None)
    c = getattr(update, "effective_chat", None)
    name = None
    if u is not None:
        name = f"@{u.username}" if u.username else u.full_name
    return {"uid": u.id if u else None, "user": name, "chat": c.id if c else None}


def _admin_id():
    try:
        return int(USER_ID)
    except (TypeError, ValueError):
        return None


def log_denied(update, reason="not_admin"):
    msg = getattr(update, "effective_message", None)
    cq = getattr(update, "callback_query", None)
    audit("auth.denied", logging.WARNING, reason=reason,
          text=getattr(msg, "text", None) if cq is None else None,
          data=cq.data if cq is not None else None, **actor(update))


def audited(name):
    """Wraps a command handler: logs who ran it, args, duration and outcome (one line, after it finishes)."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(update, context, *a, **kw):
            if getattr(update, "callback_query", None) is not None:
                return await fn(update, context, *a, **kw)      # button presses are logged by dispatch_callback
            t0 = time.perf_counter()
            outcome = "ok"
            try:
                return await fn(update, context, *a, **kw)
            except BaseException as e:
                outcome = f"error:{type(e).__name__}"
                raise
            finally:
                audit("cmd", logging.WARNING if outcome != "ok" else logging.INFO,
                      cmd=name, args=" ".join(context.args or []) or None,
                      ms=round((time.perf_counter() - t0) * 1000), result=outcome,
                      batch=True if in_batch.get() else None, **actor(update))
        return wrapper
    return deco


async def observe_message(update, context):
    """Non-command messages are otherwise ignored silently; strangers poking the bot get logged."""
    msg = update.effective_message
    if msg is None:
        return
    u = update.effective_user
    admin = u is not None and u.id == _admin_id()
    audit("message.ignored", logging.DEBUG if admin else logging.WARNING,
          text=msg.text or msg.caption, kind=None if msg.text else "non-text", **actor(update))