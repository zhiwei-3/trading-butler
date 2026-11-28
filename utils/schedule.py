# utils/schedule.py
"""Session schedule: which strategy may trade in which UTC hours. Pure.

Text form (what /set schedule takes and settings.json stores):
    zscore_reversion:0-6.5 trend_breakout:7-21
Hours are true UTC, fractional allowed. start > end wraps midnight (22-6). A bare key means all day.
Order = priority when windows overlap."""


def parse_schedule(text):
    """'a:0-6.5 b:7-21' -> [('a', 0.0, 6.5), ('b', 7.0, 21.0)]. 'off'/'none'/'' -> []. Raises ValueError."""
    s = str(text or "").strip().lower()
    if s in ("", "off", "none"):
        return []
    out = []
    for tok in s.replace(",", " ").split():
        key, _, rng = tok.partition(":")
        if not key:
            raise ValueError(f"'{tok}': missing strategy key")
        if rng in ("", "all"):
            start, end = 0.0, 24.0
        else:
            a, sep, b = rng.partition("-")
            try:
                if not sep:
                    raise ValueError
                start, end = float(a), float(b)
            except ValueError:
                raise ValueError(f"'{tok}': use key:start-end in UTC hours, e.g. {key}:0-6.5") from None
            if not (0 <= start < 24 and 0 <= end <= 24) or start == end:
                raise ValueError(f"'{tok}': hours must be 0-24 and start must differ from end")
        out.append((key, start, end))
    return out


def in_window(hour, start, end):
    """True if the UTC fractional `hour` falls in [start, end); start > end wraps midnight."""
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def schedule_text(text):
    """Setting parser: validates and returns the normalised string ('off' when empty)."""
    return " ".join(f"{k}:{a:g}-{b:g}" for k, a, b in parse_schedule(text)) or "off"