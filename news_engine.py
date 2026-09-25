# news_engine.py
import logging
import asyncio
import json
import subprocess
import requests
from datetime import datetime, timezone, timedelta
from telegram.ext import ContextTypes
from config import ALERT_STATE

# In-Memory Cache & State Variables
_NEWS_CACHE = None
_LAST_FETCH_TIME = None
_CACHE_DURATION = timedelta(minutes=10)
_ANNOUNCED_RESULTS = {}   # event_key -> added_at (UTC). Dict (not set) so it can be pruned by age.
_RESULT_TTL = timedelta(days=8)


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
        raw_text = (result.stdout or "").strip()

        # Validate that response is non-empty and starts with valid JSON characters
        if raw_text and raw_text.startswith(("{", "[")):
            return json.loads(raw_text)
        
        logging.warning("⚠️ curl fallback returned empty or non-JSON response from calendar API.")
    except json.JSONDecodeError as e:
        logging.warning(f"⚠️ Failed to parse calendar JSON from curl output: {e}")
    except Exception as e:
        logging.error(f"curl Fallback Error: {e}")
    return None


def _fetch_calendar_sync():
    """Synchronous HTTP fetcher with curl fallback, retry cooldown, and 10-minute caching."""
    global _NEWS_CACHE, _LAST_FETCH_TIME
    now = datetime.now(timezone.utc)

    # 1. Return fresh cached data if available (10-minute cache)
    if _NEWS_CACHE is not None and _LAST_FETCH_TIME and (now - _LAST_FETCH_TIME) < _CACHE_DURATION:
        return _NEWS_CACHE

    # 2. Cooldown throttle: If previous fetch failed, wait 60s before retrying to prevent log spam
    _FAILED_COOLDOWN = timedelta(seconds=60)
    if _NEWS_CACHE is None and _LAST_FETCH_TIME and (now - _LAST_FETCH_TIME) < _FAILED_COOLDOWN:
        return None

    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    data = None
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
    except Exception as e:
        logging.warning(f"Primary requests fetch failed ({e}). Attempting curl fallback...")

    if not data:
        data = _fetch_via_curl(url)

    # Always update last fetch attempt timestamp to throttle retries on failure
    _LAST_FETCH_TIME = now

    if data:
        _NEWS_CACHE = data
        return _NEWS_CACHE

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


def _to_num(v):
    """
    Parses ForexFactory-style numeric strings ('3.1%', '250K', '-0.2', 'N/A') into
    floats.

    BUGFIX: generate_xauusd_news_insight used to compare these fields as raw
    strings (`actual < forecast`), which is lexicographic, not numeric — e.g.
    "10.0" < "9.0" evaluates True. Every directional call for CPI/NFP/etc. was
    potentially wrong. Returns None (rather than raising) for missing/"N/A"
    values so callers can fall back to the neutral message.
    """
    if v is None:
        return None
    s = str(v).strip().replace("%", "").replace(",", "")
    if not s or s.upper() in ("N/A", "NA"):
        return None
    mult = 1.0
    if s[-1].upper() in ("K", "M", "B"):
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}[s[-1].upper()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def generate_xauusd_news_insight(event_title: str, forecast: str, previous: str, actual: str = None) -> str:
    """
    Generates directional and volatility insights for XAUUSD based on economic metrics.
    """
    title_upper = event_title.upper()
    actual_n = _to_num(actual)
    forecast_n = _to_num(forecast)

    # 1. Inflation & Growth (CPI, PPI, PCE, GDP, Retail Sales)
    if any(k in title_upper for k in ["CPI", "PPI", "PCE", "GDP", "RETAIL SALES"]):
        if actual_n is not None and forecast_n is not None:
            return "📈 **Bullish XAUUSD** (Lower inflation/growth weakens USD)" if actual_n < forecast_n else "📉 **Bearish XAUUSD** (Higher inflation/growth strengthens USD)"
        return "💡 *Higher actual figures strengthen USD (Bearish Gold); lower figures weaken USD (Bullish Gold).*"

    # 2. Employment Data (NFP, Non-Farm, ADP, Employment Change)
    elif any(k in title_upper for k in ["NFP", "NON-FARM", "EMPLOYMENT", "ADP"]):
        if actual_n is not None and forecast_n is not None:
            return "📈 **Bullish XAUUSD** (Weaker labor market weakens USD)" if actual_n < forecast_n else "📉 **Bearish XAUUSD** (Strong labor market boosts USD)"
        return "💡 *Strong job growth boosts Fed rate hike expectations (Bearish Gold).* "

    # 3. Unemployment Claims & Unemployment Rate
    elif "UNEMPLOYMENT" in title_upper:
        if actual_n is not None and forecast_n is not None:
            return "📈 **Bullish XAUUSD** (Higher unemployment hurts USD)" if actual_n > forecast_n else "📉 **Bearish XAUUSD** (Lower unemployment supports USD)"
        return "💡 *Higher unemployment weakens USD (Bullish Gold).* "

    # 4. Central Bank Rates (FOMC, Federal Funds Rate)
    elif any(k in title_upper for k in ["FOMC", "FED", "RATE"]):
        return "⚡ **High Volatility Risk!** Rate hikes/hawkish stance = Bearish Gold 📉; Rate cuts/dovish stance = Bullish Gold 📈."

    return "💡 *High market volatility expected upon release.*"


def _clean_val(val):
    """Formats missing or blank JSON values cleanly."""
    if not val or str(val).strip() == "":
        return "N/A"
    return str(val).strip()


async def news_guard_check(context: ContextTypes.DEFAULT_TYPE, chat_id):
    """
    Sends an enriched pre-event warning ping ~30 minutes before configured news releases,
    including forecast, previous metrics, and XAUUSD strategic insights.
    """
    target_impacts = [imp.lower() for imp in ALERT_STATE.get("news_blockade_impacts", ["high"])]
    all_events = await fetch_economic_events(impact_level="all", currency="USD")
    if not all_events:
        return

    events = [ev for ev in all_events if str(ev.get("impact", "")).strip().lower() in target_impacts]
    if not events:
        return

    now_utc = datetime.now(timezone.utc)

    warned = ALERT_STATE["news_warned_events"]
    cutoff = now_utc - _RESULT_TTL
    for k in [k for k, t in warned.items() if t < cutoff]:
        del warned[k]

    for ev in events:
        event_title = ev.get("title", "USD Economic Event")
        raw_date = ev.get("date", "")
        try:
            clean_date = raw_date.replace("Z", "+00:00")
            event_dt = datetime.fromisoformat(clean_date).astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue

        time_diff = (event_dt - now_utc).total_seconds() / 60.0
        warn_key = f"{event_title}|{raw_date}"

        if 25 <= time_diff <= 35 and warn_key not in warned:
            warned[warn_key] = now_utc
            
            # Format local time and timezone label
            dt_local = event_dt.astimezone()
            tz_label = dt_local.strftime("%Z") or "Local"
            time_str = f"{dt_local.strftime('%H:%M')} {tz_label} ({event_dt.strftime('%H:%M UTC')})"

            forecast = _clean_val(ev.get("forecast"))
            previous = _clean_val(ev.get("previous"))
            insight = generate_xauusd_news_insight(event_title, forecast, previous)

            impact_badge = str(ev.get("impact", "")).upper()
            msg = (
                f"🚨 **{impact_badge} IMPACT NEWS HEADS-UP** 🚨\n\n"
                f"• **Event:** `{event_title}`\n"
                f"• **Time:** `{time_str}` (~30 mins away)\n"
                f"• **Forecast:** `{forecast}` | **Previous:** `{previous}`\n\n"
                f"📊 **XAUUSD Macro Insight:**\n{insight}\n\n"
                f"🛡️ *Scanner will auto-pause during release. Tighten SL or lock profits.*"
            )
            try:
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            except Exception as e:
                logging.warning(f"Failed to send news warning: {e}")


async def check_news_post_release(context: ContextTypes.DEFAULT_TYPE, chat_id):
    """
    Monitors calendar feed for newly published actual results and broadcasts live outcome analysis.
    """
    global _ANNOUNCED_RESULTS
    target_impacts = [imp.lower() for imp in ALERT_STATE.get("news_blockade_impacts", ["high"])]
    all_events = await fetch_economic_events(impact_level="all", currency="USD")
    if not all_events:
        return

    events = [ev for ev in all_events if str(ev.get("impact", "")).strip().lower() in target_impacts]
    if not events:
        return

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - _RESULT_TTL
    _ANNOUNCED_RESULTS = {k: t for k, t in _ANNOUNCED_RESULTS.items() if t >= cutoff}

    for ev in events:
        actual = ev.get("actual")
        if not actual or str(actual).strip() == "":
            continue

        event_title = ev.get("title", "USD News Event")
        raw_date = ev.get("date", "")
        event_key = f"RESULT|{event_title}|{raw_date}"

        if event_key in _ANNOUNCED_RESULTS:
            continue

        try:
            clean_date = raw_date.replace("Z", "+00:00")
            event_dt = datetime.fromisoformat(clean_date).astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue

        minutes_since_release = (now_utc - event_dt).total_seconds() / 60.0
        if 0 <= minutes_since_release <= 60:
            _ANNOUNCED_RESULTS[event_key] = now_utc
            forecast = _clean_val(ev.get("forecast"))
            previous = _clean_val(ev.get("previous"))
            actual_str = _clean_val(actual)

            outcome_insight = generate_xauusd_news_insight(event_title, forecast, previous, actual=actual_str)

            msg = (
                f"📊 **ECONOMIC NEWS RESULT DISPATCH** 📊\n\n"
                f"• **Event:** `{event_title}`\n"
                f"• **Actual:** `{actual_str}`\n"
                f"• **Forecast:** `{forecast}` | **Previous:** `{previous}`\n\n"
                f"⚡ **XAUUSD Market Impact:**\n{outcome_insight}\n\n"
                f"🔎 *Monitor MT5 price structure for post-news momentum or mean-reversion signals.*"
            )
            try:
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            except Exception as e:
                logging.warning(f"Failed to send news result dispatch: {e}")


async def check_news_blockade():
    """
    Checks if current UTC time falls within the configured blackout window.
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