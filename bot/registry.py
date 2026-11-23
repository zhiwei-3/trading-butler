# bot/registry.py
"""
Self-registration for Telegram commands, callback buttons and scheduled jobs.

  @command("news", "USD Calendar (day + impact)", icon="🗓️", group="Market Analysis", usage="/news [day] [impact]")
  @callback("btn_gold")                          # exact callback_data
  @callback("set_strat_", prefix=True)           # callback_data prefix
  @job("xauusd_scanner", interval=60, first=5, enabled=lambda: ALERT_STATE["scanner_enabled"])

app.py never lists handlers: it calls install(), bot_commands() and start_all_jobs().
/start's help text and Telegram's command hints are generated from the same metadata.
"""

import logging
from dataclasses import dataclass
from typing import Callable
import functools
import time

from telegram import BotCommand, Update
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from bot.auth import admin_only
from bot.audit import audited, actor, observe_message
from utils.logkit import audit, system

# group -> icon, in display order. Commands in an unlisted group are shown after these.
GROUPS = {
    "Core & Dashboards": "🎛️",
    "Trade Execution & History": "🤖",
    "Market Analysis": "📊",
    "Strategy & Parameters": "⚙️",
    "Testing & Utility": "🧪",
}


# ---------------------------------------------------------------- commands

@dataclass(frozen=True)
class CommandSpec:
    name: str
    fn: Callable
    help: str
    icon: str
    group: str
    usage: str
    order: int
    aliases: tuple
    listed: bool


COMMANDS = {}


def command(name, help, *, icon="▫️", group="Other", usage=None, order=50, aliases=(), listed=True):
    """Registers a slash command (admin-only) and returns the wrapped handler."""
    def deco(fn):
        if name in COMMANDS:
            raise ValueError(f"Duplicate command /{name}")
        wrapped = admin_only(audited(name)(fn))
        COMMANDS[name] = CommandSpec(name, wrapped, help, icon, group, usage or f"/{name}",
                                     order, tuple(aliases), listed)
        return wrapped
    return deco


def _ordered():
    groups = list(GROUPS)
    def key(s):
        return (groups.index(s.group) if s.group in groups else len(groups), s.group, s.order, s.name)
    return sorted(COMMANDS.values(), key=key)


def bot_commands():
    """Autocomplete hints for Telegram's command menu."""
    return [BotCommand(s.name, f"{s.icon} {s.help}"[:256]) for s in _ordered()]


def help_text():
    """The /start guide, generated from the registered commands."""
    ordered = _ordered()
    groups = list(GROUPS) + sorted({s.group for s in ordered if s.group not in GROUPS})
    lines = ["🤵‍♂️ **Trading Butler Online (Full Stack Modular)**\n"]
    for group in groups:
        specs = [s for s in ordered if s.group == group and s.listed]
        if not specs:
            continue
        lines.append(f"**{GROUPS.get(group, '▫️')} {group}**")
        lines += [f"• `{s.usage}` - {s.help}" for s in specs]
        lines.append("")
    lines.append("💓 24/7 monitoring is active in this chat.")
    return "\n".join(lines)


# ---------------------------------------------------------------- callback buttons

@dataclass(frozen=True)
class CallbackSpec:
    fn: Callable
    answers: bool


_CB_EXACT, _CB_PREFIX = {}, {}


def callback(*matches, prefix=False, answers=True):
    """Registers a callback_data handler. answers=False when the handler calls query.answer() itself."""
    def deco(fn):
        table = _CB_PREFIX if prefix else _CB_EXACT
        for m in matches:
            if m in table:
                raise ValueError(f"Duplicate callback '{m}'")
            table[m] = CallbackSpec(fn, answers)
        return fn
    return deco


def _find_callback(data):
    spec = _CB_EXACT.get(data)
    if spec:
        return spec
    for pref in sorted(_CB_PREFIX, key=len, reverse=True):
        if data.startswith(pref):
            return _CB_PREFIX[pref]
    return None


@admin_only
async def dispatch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    spec = _find_callback(data)
    if spec is None:
        await query.answer()
        logging.warning(f"Unhandled callback_data: {data!r}")
        audit("callback", logging.WARNING, data=data, result="unhandled", **actor(update))
        return
    if spec.answers:
        await query.answer()
    t0 = time.perf_counter()
    outcome = "ok"
    try:
        await spec.fn(update, context)
    except BadRequest as e:
        # Re-selecting the current option re-renders an identical message.
        if "not modified" in str(e).lower():
            outcome = "not_modified"
        else:
            outcome = "error:BadRequest"
            raise
    except BaseException as e:
        outcome = f"error:{type(e).__name__}"
        raise
    finally:
        audit("callback", logging.WARNING if outcome.startswith("error") else logging.INFO,
              data=data, ms=round((time.perf_counter() - t0) * 1000), result=outcome, **actor(update))


# ---------------------------------------------------------------- scheduled jobs

@dataclass(frozen=True)
class JobSpec:
    name: str
    fn: Callable
    interval: object            # seconds, or a zero-arg callable returning seconds
    first: float
    enabled: object             # None (always) or a zero-arg callable
    grace: int


JOBS = {}
_SLOW_JOB_S = 15.0

def _logged_job(name, fn):
    """Times every run; logs failures (the traceback comes from app.error_handler) and slow runs."""
    @functools.wraps(fn)
    async def wrapper(context):
        t0 = time.perf_counter()
        try:
            return await fn(context)
        except Exception as e:
            system("job.failed", logging.WARNING, job=name, error=type(e).__name__,
                   ms=round((time.perf_counter() - t0) * 1000))
            raise
        finally:
            dt = time.perf_counter() - t0
            if dt > _SLOW_JOB_S:
                system("job.slow", logging.WARNING, job=name, seconds=round(dt, 1))
            else:
                logging.debug(f"job {name} finished in {dt * 1000:.0f}ms")
    return wrapper


def job(name, *, interval, first=10, enabled=None, grace=30):
    """Registers a repeating job. `enabled` is checked every time the job is (re)started."""
    def deco(fn):
        if name in JOBS:
            raise ValueError(f"Duplicate job '{name}'")
        JOBS[name] = JobSpec(name, _logged_job(name, fn), interval, first, enabled, grace)
        return fn
    return deco


def start_job(job_queue, name, chat_id):
    """Starts the job unless it is disabled or already running. Returns True if it is running afterwards."""
    spec = JOBS[name]
    if spec.enabled is not None and not spec.enabled():
        return False
    if job_queue.get_jobs_by_name(name):
        return True
    interval = spec.interval() if callable(spec.interval) else spec.interval
    job_queue.run_repeating(spec.fn, interval=interval, first=spec.first, chat_id=chat_id, name=name,
                            job_kwargs={"misfire_grace_time": spec.grace})
    system("job.start", job=name, interval_s=interval)
    return True


def stop_job(job_queue, name):
    jobs = job_queue.get_jobs_by_name(name)
    for j in jobs:
        j.schedule_removal()
    if jobs:
        system("job.stop", job=name)


def restart_job(job_queue, name, chat_id):
    """Stop + start (picks up a changed interval / enabled flag)."""
    stop_job(job_queue, name)
    return start_job(job_queue, name, chat_id)


def start_all_jobs(job_queue, chat_id):
    for name in JOBS:
        start_job(job_queue, name, chat_id)


# ---------------------------------------------------------------- wiring

def install(app: Application):
    from bot.batch import batch_handler        # lazy: batch imports COMMANDS from this module
    # group -1 runs before the CommandHandlers (group 0); it only consumes multi-line command messages.
    app.add_handler(MessageHandler(filters.COMMAND & filters.UpdateType.MESSAGES, batch_handler), group=-1)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, observe_message), group=-2)
    for spec in COMMANDS.values():
        app.add_handler(CommandHandler([spec.name, *spec.aliases], spec.fn))
    app.add_handler(CallbackQueryHandler(dispatch_callback))