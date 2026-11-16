# bot/messaging.py
"""Telegram send/edit helpers: Markdown first, plain-text fallback, never raise on delivery failure."""

import logging

from telegram.error import BadRequest, TelegramError

SAFE_BUDGET = 3900       # leave headroom under Telegram's 4096-char message limit
CAPTION_LIMIT = 1024


def md_safe(text) -> str:
    """Strips legacy-Markdown control characters from dynamic text (broker comments,
    error strings) so a stray '_' or '*' can't make Telegram reject the whole reply."""
    return str(text).replace("*", "").replace("_", " ").replace("`", "'").replace("[", "(")


def strip_md(text) -> str:
    """Plain-text version of a Markdown message (used when Telegram rejects the formatting)."""
    return str(text).replace("*", "").replace("`", "").replace("_", " ")


def fit_lines(lines, budget=SAFE_BUDGET, note="\n... *(truncated)*", sep="\n"):
    """Joins lines until the budget is reached, then appends `note`."""
    out, used = [], 0
    for line in lines:
        if used + len(line) + len(sep) > budget:
            out.append(note)
            break
        out.append(line)
        used += len(line) + len(sep)
    return sep.join(out)


async def send_text(bot, chat_id, text, **kw):
    """Background-job message: Markdown, falling back to plain text. Delivery failures are logged, not raised."""
    kw.pop("parse_mode", None)
    try:
        try:
            return await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", **kw)
        except BadRequest as e:
            logging.warning(f"Markdown rejected ({e}); resending as plain text")
            return await bot.send_message(chat_id=chat_id, text=strip_md(text), **kw)
    except TelegramError as e:
        logging.warning(f"⚠️ Telegram send failed: {e}")
        return None


async def send_alert(bot, chat_id, text, photo=None):
    """Alert with an optional chart: photo+caption when it fits, otherwise photo then text."""
    try:
        if photo and len(text) <= CAPTION_LIMIT:
            try:
                await bot.send_photo(chat_id=chat_id, photo=photo, caption=text, parse_mode="Markdown")
            except BadRequest:
                photo.seek(0)
                await bot.send_photo(chat_id=chat_id, photo=photo, caption=strip_md(text))
            return
        if photo:
            await bot.send_photo(chat_id=chat_id, photo=photo)
        await send_text(bot, chat_id, text)
    except TelegramError as e:
        logging.warning(f"⚠️ Alert dispatch failed: {e}")


async def reply_md(message, text, reply_markup=None):
    """Command reply: Markdown with plain-text fallback."""
    try:
        return await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except BadRequest as e:
        logging.warning(f"Markdown rejected ({e}); resending as plain text")
        return await message.reply_text(strip_md(text), reply_markup=reply_markup)


async def reply_photo_md(message, photo, caption):
    try:
        return await message.reply_photo(photo=photo, caption=caption, parse_mode="Markdown")
    except BadRequest:
        photo.seek(0)
        return await message.reply_photo(photo=photo, caption=strip_md(caption))


async def edit_md(query, text, reply_markup=None):
    """Button-driven edit: Markdown with plain fallback; 'message is not modified' is ignored."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        logging.warning(f"Markdown rejected ({e}); resending as plain text")
        try:
            await query.edit_message_text(strip_md(text), reply_markup=reply_markup)
        except BadRequest as e2:
            if "not modified" not in str(e2).lower():
                raise