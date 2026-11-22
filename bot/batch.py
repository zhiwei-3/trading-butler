# bot/batch.py
"""
Multi-line command batches: several settings commands in ONE Telegram message.

    /filters structure on
    /filters volume off
    /set rsi_reversion.rsi_buy 30
    /set rsi_reversion.rsi_sell 70

A message with 2+ command lines (from the admin) is validated as a whole, then each line is run
through its normal registered handler. Their replies are captured and returned as ONE message.
Only commands in BATCHABLE can be batched (no order execution / no long-running jobs).
"""

import logging

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from config import USER_ID
from bot.registry import COMMANDS
from bot.messaging import reply_md, fit_lines

BATCHABLE = {"set", "filters", "watchlist", "timeframe", "strategy",
             "heartbeat", "scanner_on", "scanner_off"}
MAX_LINES = 25


class _CaptureMessage:
    """Stands in for update.message: reply_text() is captured instead of sent."""
    def __init__(self, real, sink):
        self._real, self._sink = real, sink

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def reply_text(self, text, *args, **kwargs):
        self._sink.append(str(text).strip())
        return None


class _CaptureUpdate:
    def __init__(self, real, msg):
        self._real = real
        self.message = msg
        self.effective_message = msg

    def __getattr__(self, name):
        return getattr(self._real, name)


def _lookup():
    table = {}
    for spec in COMMANDS.values():
        for n in (spec.name, *spec.aliases):
            table[n] = spec
    return table


def _parse(text):
    """-> (lines, error). lines = [(raw_line, spec, args)]. Blank lines and '#' comments are skipped."""
    table = _lookup()
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("/"):
            return None, f"`{line[:40]}` is not a command (every line must start with `/`)."
        tokens = line.split()
        name = tokens[0][1:].split("@")[0].lower()
        spec = table.get(name)
        if spec is None:
            return None, f"Unknown command `/{name}`."
        if spec.name not in BATCHABLE:
            return None, (f"`/{name}` can't be batched. Batchable: "
                          + ", ".join(f"`/{n}`" for n in sorted(BATCHABLE)) + ".")
        out.append((line, spec, tokens[1:]))
    if len(out) > MAX_LINES:
        return None, f"Too many commands in one message (max {MAX_LINES})."
    return out, None


async def batch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or not msg.text or "\n" not in msg.text.strip():
        return                                              # normal single-command path

    # Non-admins fall through so the regular handler answers "Access Denied" exactly once.
    try:
        allowed = int(USER_ID)
    except (TypeError, ValueError):
        return
    if update.effective_user is None or update.effective_user.id != allowed:
        return

    cmd_lines = [l for l in msg.text.splitlines() if l.strip().startswith("/")]
    if len(cmd_lines) < 2:
        return

    parsed, err = _parse(msg.text)
    if err:
        await reply_md(msg, f"❌ **Batch rejected — nothing was applied.**\n{err}")
        raise ApplicationHandlerStop

    results = []
    for line, spec, args in parsed:
        sink = []
        proxy = _CaptureUpdate(update, _CaptureMessage(msg, sink))
        context.args = args
        try:
            await spec.fn(proxy, context)
        except Exception as e:
            logging.exception(f"Batch line failed: {line}")
            sink.append(f"❌ Crashed: {str(e)[:200]}")
        results.append((line, sink or ["(no reply)"]))

    lines = [f"🧾 **BATCH — {len(results)} command(s)**", ""]
    for line, outs in results:
        lines.append(f"▸ `{line.replace('`', chr(39))}`")
        lines.extend(outs)
        lines.append("")
    await reply_md(msg, fit_lines(lines, note="\n... *(truncated)*"))
    raise ApplicationHandlerStop