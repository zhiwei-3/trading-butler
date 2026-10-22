from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes
from config import USER_ID


def md_safe(text) -> str:
    """Strips legacy-Markdown control characters from dynamic text (broker comments,
    error strings) so a stray '_' or '*' can't make Telegram reject the whole reply."""
    return str(text).replace("*", "").replace("_", " ").replace("`", "'").replace("[", "(")


def admin_only(func):
    """Decorator to restrict command access strictly to USER_ID."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            allowed_id = int(USER_ID)
        except (TypeError, ValueError):
            if update.effective_message:
                await update.effective_message.reply_text("⛔ **Bot misconfigured:** TELEGRAM_USER_ID is not set.")
            return
        user_id = update.effective_user.id if update.effective_user else None
        if user_id != allowed_id:
            if update.callback_query:
                try:
                    await update.callback_query.answer("⛔ Access denied", show_alert=True)
                except Exception:
                    pass
            elif update.effective_message:
                await update.effective_message.reply_text("⛔ **Access Denied:** You are not authorized to use this bot.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper