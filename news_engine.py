import logging
import asyncio
import json
import subprocess
import requests
from datetime import datetime, timezone, timedelta
from telegram.ext import ContextTypes
from config import ALERT_STATE

# In-Memory Cache Variables
_NEWS_CACHE = None
_LAST_FETCH_TIME = None
_CACHE_DURATION = timedelta(minutes=15)

def _fetch_via_curl(url):
    """Fallback fetcher using native system curl to bypass Python OpenSSL TLS blocks."""
    try:
        cmd = [
            "curl", "-s", "-L",
            "--max-time", "8",
            "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "-H", "Accept: application/json",
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        if result.stdout:
            return json.loads(result.stdout)
    except Exception as e:
        logging.error(f"curl Fallback Error: {e}")
    return None

def _fetch_calendar_sync():
    """Synchronous HTTP fetcher with curl fallback and 15-minute caching."""
    global _NEWS_CACHE, _LAST_FETCH_TIME
    now = datetime.now(timezone.utc)

    # Return cached data if still valid
    if _NEWS_CACHE is not None and _LAST_FETCH_TIME and (now - _LAST_FETCH_TIME) < _CACHE_DURATION:
        return _NEWS_CACHE

    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    data = None
    # Primary Attempt: Python requests
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
    except Exception as e:
        logging.warning(f"Primary requests fetch failed ({e}). Attempting curl fallback...")

    # Secondary Attempt: System curl fallback (bypasses SSL EOF issues)
    if not data:
        data = _fetch_via_curl(url)

    # Update cache if successful
    if data:
        _NEWS_CACHE = data
        _LAST_FETCH_TIME = now
        return _NEWS_CACHE

    # Return stale cache during total outages if available
    return _NEWS_CACHE if _NEWS_CACHE is not None else None

async def fetch_economic_events(impact_level="high", currency="USD"):
    """Asynchronous fetcher returning filtered economic events or None on failure."""
    calendar = await asyncio.to_thread(_fetch_calendar_sync)
    if calendar is None:
        return None

    target_impact = str(impact_level).strip().lower()
    target_currency = str(currency).strip().upper()

    filtered_events = []
    for ev in calendar:
        ev_country = str(ev.get("country", "")).strip().upper()
        ev_impact = str(ev.get("impact", "")).strip().lower()

        currency_match = (target_currency == "ALL" or ev_country == target_currency)
        impact_match = (target_impact == "all" or ev_impact == target_impact)

        if currency_match and impact_match:
            filtered_events.append(ev)

    return filtered_events

async def news_guard_check(context: ContextTypes.DEFAULT_TYPE, chat_id):
    """Sends a Telegram warning ping ~30 minutes before high-impact news releases."""
    events = await fetch_economic_events(impact_level="high", currency="USD")
    if not events:
        return

    now_utc = datetime.now(timezone.utc)

    for ev in events:
        event_title = ev.get("title", "USD High Impact Event")
        raw_date = ev.get("date", "")
        try:
            # Robust ISO parsing supporting 'Z' suffix
            clean_date = raw_date.replace("Z", "+00:00")
            event_dt = datetime.fromisoformat(clean_date).astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue

        time_diff = (event_dt - now_utc).total_seconds() / 60.0
        warn_key = f"{event_title}|{raw_date}"

        # Dispatch warning message 25-35 minutes prior to release
        if 25 <= time_diff <= 35 and warn_key not in ALERT_STATE["news_warned_events"]:
            ALERT_STATE["news_warned_events"].add(warn_key)
            time_str = event_dt.strftime("%Y-%m-%d %H:%M UTC")
            msg = (
                f"⚠️ **HIGH IMPACT NEWS WARNING** ⚠️\n\n"
                f"• **Event:** {event_title}\n"
                f"• **Scheduled Time:** `{time_str}` (~30 mins away)\n\n"
                f"💡 *Consider tightening Stop Losses or securing open profits.*"
            )
            try:
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            except Exception as e:
                logging.warning(f"Failed to send news warning: {e}")

async def check_news_blockade():
    """
    Checks if current UTC time falls within the configured blackout window
    for upcoming or recent USD economic events.
    Returns: (is_blocked: bool, reason_string: str)
    """
    if not ALERT_STATE.get("news_blockade_enabled", True):
        return False, ""

    target_impacts = [imp.lower() for imp in ALERT_STATE.get("news_blockade_impacts", ["high"])]
    mins_before = ALERT_STATE.get("news_blockade_mins_before", 30)
    mins_after = ALERT_STATE.get("news_blockade_mins_after", 15)

    events = await fetch_economic_events(impact_level="all", currency="USD")
    if not events:
        return False, ""

    now_utc = datetime.now(timezone.utc)

    for ev in events:
        ev_impact = str(ev.get("impact", "")).strip().lower()
        if ev_impact not in target_impacts:
            continue

        raw_date = ev.get("date", "")
        try:
            clean_date = raw_date.replace("Z", "+00:00")
            event_dt = datetime.fromisoformat(clean_date).astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue

        block_start = event_dt - timedelta(minutes=mins_before)
        block_end = event_dt + timedelta(minutes=mins_after)

        if block_start <= now_utc <= block_end:
            title = ev.get("title", "USD News Event")
            event_time_str = event_dt.strftime("%H:%M UTC")
            reason = f"{ev_impact.upper()} Impact: {title} @ {event_time_str}"
            return True, reason

    return False, ""