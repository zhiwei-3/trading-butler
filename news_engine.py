import logging
import requests
from datetime import datetime, timezone
from telegram.ext import ContextTypes
from config import ALERT_STATE

def fetch_economic_events(impact_level="high", currency="USD"):
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return []
        calendar = resp.json()
    except Exception as e:
        logging.error(f"News Fetch Error: {e}")
        return []

    impact_level = impact_level.lower()
    currency = currency.upper()
    return [
        ev for ev in calendar
        if (currency == "ALL" or str(ev.get("country")).upper() == currency) and
           (impact_level == "all" or str(ev.get("impact")).lower() == impact_level)
    ]

async def news_guard_check(context: ContextTypes.DEFAULT_TYPE, chat_id):
    events = fetch_economic_events(impact_level="high", currency="USD")
    if not events:
        return False

    now_utc = datetime.now(timezone.utc)
    in_lockout_period = False

    for ev in events:
        event_title = ev.get("title", "USD High Impact Event")
        raw_date = ev.get("date", "")
        try:
            event_dt = datetime.fromisoformat(raw_date).astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue

        time_diff = (event_dt - now_utc).total_seconds() / 60.0

        if 25 <= time_diff <= 35 and event_title not in ALERT_STATE["news_warned_events"]:
            ALERT_STATE["news_warned_events"].add(event_title)
            time_str = event_dt.strftime("%Y-%m-%d %H:%M UTC")
            msg = (
                f"⚠️ **HIGH IMPACT NEWS WARNING** ⚠️\n\n"
                f"• **Event:** {event_title}\n"
                f"• **Scheduled Time:** `{time_str}` (~30 mins away)\n\n"
                f"💡 *Consider tightening Stop Losses or securing open profits.*"
            )
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

        if -15 <= time_diff <= 5:
            in_lockout_period = True

    ALERT_STATE["news_lockout"] = in_lockout_period
    return in_lockout_period