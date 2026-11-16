from bot.auth import admin_only  # noqa: F401  (re-exported so existing imports keep working)


def md_safe(text) -> str:
    """Strips legacy-Markdown control characters from dynamic text (broker comments,
    error strings) so a stray '_' or '*' can't make Telegram reject the whole reply."""
    return str(text).replace("*", "").replace("_", " ").replace("`", "'").replace("[", "(")