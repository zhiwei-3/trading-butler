# utils/fmt.py
"""Small pure display helpers shared by the Telegram commands."""


def money(v, curr):
    return f"{v:+,.2f} {curr}"


def pf_text(st):
    """Profit factor text from a deal_stats() dict: '∞' when there are wins but no losses, '—' with no wins."""
    if st["pf"] is not None:
        return f"{st['pf']}"
    return "∞" if st["wins"] else "—"
