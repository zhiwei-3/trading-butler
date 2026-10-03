import asyncio
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, TF_LABELS
from mt5_engine import get_gold_symbol, get_tick
from news_engine import fetch_economic_events
from strategy.evaluator import analyze_market
from strategy.chart import generate_chart_snapshot
from ._common import admin_only


# ---------------------------------------------------------------- /news helpers

_NEWS_IMPACTS = ("high", "medium", "low", "holiday", "all")
_NEWS_RELATIVE_DAYS = {"yesterday": -1, "today": 0, "tomorrow": 1}
_NEWS_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_NEWS_IMPACT_EMOJI = {"high": "🔴", "medium": "🟠", "low": "🟡", "holiday": "⚪"}


def _news_resolve_day(token):
    """Maps 'today'/'tomorrow'/'yesterday'/'tuesday'/'tue' to a LOCAL calendar date
    inside the current Mon–Sun week (weekday names always mean *this* week, since
    the calendar feed only covers the current week). Returns None if unrecognised."""
    today = datetime.now().astimezone().date()
    if token in _NEWS_RELATIVE_DAYS:
        return today + timedelta(days=_NEWS_RELATIVE_DAYS[token])
    if token in _NEWS_WEEKDAYS:
        monday = today - timedelta(days=today.weekday())
        return monday + timedelta(days=_NEWS_WEEKDAYS[token])
    return None


def _news_md_escape(text):
    """Keeps event titles from breaking Telegram's legacy Markdown parser."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def _news_usage_text():
    return (
        "⚠️ **Usage:** `/news [day] [impact]`\n\n"
        "**Day:** `today`, `yesterday`, `tomorrow`, or a weekday "
        "(`monday`…`sunday`, or `mon`…`sun`)\n"
        "**Impact:** `high`, `medium`, `low`, `holiday`, `all`\n\n"
        "**Examples:**\n"
        "• `/news` — whole week, HIGH impact\n"
        "• `/news today` — every impact level today\n"
        "• `/news tuesday` — every impact level on Tuesday\n"
        "• `/news friday high` — only HIGH impact on Friday\n"
        "• `/news medium` — whole week, MEDIUM impact"
    )


@admin_only
async def news_calendar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tokens = [a.strip().lower() for a in (context.args or []) if a.strip()]

    target_date = None
    day_token = None
    impact = None

    for tok in tokens:
        if tok in _NEWS_IMPACTS and impact is None:
            impact = tok
        elif target_date is None and _news_resolve_day(tok) is not None:
            target_date = _news_resolve_day(tok)
            day_token = tok
        else:
            await update.message.reply_text(_news_usage_text(), parse_mode="Markdown")
            return

    day_mode = target_date is not None
    # Day view defaults to EVERY impact level; legacy week view defaults to HIGH.
    if impact is None:
        impact = "all" if day_mode else "high"

    events = await fetch_economic_events(impact_level=impact, currency="USD")

    if events is None:
        await update.message.reply_text(
            "📡 **Network Error:** Unable to reach economic calendar server. Please try again in a few moments.",
            parse_mode="Markdown"
        )
        return

    # Parse every event into local time once, drop unparseable ones.
    parsed = []
    for ev in events:
        raw_date = ev.get("date", "")
        try:
            dt_utc = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).astimezone(timezone.utc)
            parsed.append((dt_utc.astimezone(), ev))
        except (ValueError, TypeError):
            continue
    parsed.sort(key=lambda x: x[0])

    if day_mode:
        parsed = [(dt, ev) for dt, ev in parsed if dt.date() == target_date]

    tz_label = datetime.now().astimezone().strftime("%Z") or "LOCAL TIME"
    today_local = datetime.now().astimezone().date()

    if not parsed:
        if day_mode:
            pretty = target_date.strftime("%A, %b %d")
            note = ""
            if target_date < today_local - timedelta(days=0) and day_token != "today":
                note = "\n\n*The calendar feed only covers the current week.*"
            await update.message.reply_text(
                f"🟢 **No `{impact.upper()}` impact USD events on {pretty}.**{note}",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"🟢 **No `{impact.upper()}` impact USD events found for this week.**",
                parse_mode="Markdown"
            )
        return

    # Group by local date
    events_by_date = {}
    for dt_local, ev in parsed:
        events_by_date.setdefault(dt_local.date(), []).append((dt_local, ev))

    if day_mode:
        rel = {0: " (Today)", 1: " (Tomorrow)", -1: " (Yesterday)"}.get((target_date - today_local).days, "")
        header = (
            f"🗓️ **USD ECONOMIC CALENDAR — {target_date.strftime('%A, %b %d')}{rel}**\n"
            f"*{impact.upper()} impact | {tz_label} | {len(parsed)} event(s)*\n\n"
        )
    else:
        header = f"🗓️ **WEEKLY USD ECONOMIC CALENDAR ({impact.upper()} IMPACT | {tz_label})**\n\n"

    lines = [header]
    for date_key in sorted(events_by_date):
        if not day_mode:
            lines.append(f"📅 **{date_key.strftime('%A, %b %d')}**\n")
        for dt_local, ev in events_by_date[date_key]:
            title = _news_md_escape(ev.get("title", "N/A"))
            ev_imp = str(ev.get("impact", "")).strip().lower()
            badge = _NEWS_IMPACT_EMOJI.get(ev_imp, "⚪")
            forecast = str(ev.get("forecast", "") or "").strip()
            prev = str(ev.get("previous", "") or "").strip()
            actual = str(ev.get("actual", "") or "").strip()

            details = []
            if actual:
                details.append(f"Act: {actual}")
            if forecast:
                details.append(f"FC: {forecast}")
            if prev:
                details.append(f"Prev: {prev}")
            extra = f" ({' | '.join(details)})" if details else ""

            lines.append(f"  {badge} `{dt_local.strftime('%H:%M')}` — {title}{extra}\n")
        lines.append("\n")

    # Telegram caps messages at 4096 chars — truncate cleanly instead of failing.
    budget, out, used = 3900, [], 0
    for line in lines:
        if used + len(line) > budget:
            out.append("... *(truncated — narrow with a day or impact filter)*")
            break
        out.append(line)
        used += len(line)

    await update.message.reply_text("".join(out), parse_mode="Markdown")


# ---------------------------------------------------------------- market snapshots

@admin_only
async def spread_check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = await asyncio.to_thread(get_gold_symbol)
    if not symbol:
        await update.effective_message.reply_text("❌ MT5 Gold symbol not found.")
        return
    tick = await asyncio.to_thread(get_tick, symbol)
    if not tick:
        await update.effective_message.reply_text("❌ Unable to fetch live tick data.")
        return

    bid, ask = round(tick.bid, 2), round(tick.ask, 2)
    spread_pips = round((ask - bid) * 10, 1)
    status_msg = "🟢 NORMAL" if spread_pips <= ALERT_STATE["max_allowed_spread_pips"] else "⚠️ HIGH (SCANNER PAUSED)"

    reply = (
        f"📊 **XAUUSD SPREAD CHECK**\n\n"
        f"• **Bid:** `${bid}` | **Ask:** `${ask}`\n"
        f"• **Spread:** `{spread_pips} pips` (Limit: `{ALERT_STATE['max_allowed_spread_pips']} pips`)\n"
        f"• **Status:** {status_msg}"
    )
    await update.effective_message.reply_text(reply, parse_mode="Markdown")


@admin_only
async def gold_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /gold command and menu button callbacks."""
    symbol = await asyncio.to_thread(get_gold_symbol) or "XAUUSD"

    # update.effective_message handles both slash commands and callback buttons safely
    await update.effective_message.reply_chat_action("upload_photo")
    analysis = await asyncio.to_thread(analyze_market, symbol)

    if not analysis:
        await update.effective_message.reply_text("❌ Failed to fetch MT5 market data for Gold.")
        return

    close_p = analysis["close_price"]
    rsi_p = analysis["rsi_val"]
    atr_p = analysis["atr_val"]
    tf_lbl = analysis["tf_label"]
    struct = analysis["structure"]

    caption = (
        f"📊 **XAUUSD REAL-TIME MARKET SNAPSHOT**\n\n"
        f"• **Timeframe:** `{tf_lbl}` | **Price:** `${close_p:.2f}`\n"
        f"• **RSI (14):** `{rsi_p:.1f}` | **ATR (14):** `${atr_p:.2f}`\n"
        f"• **Structure:** `{struct}`\n"
        f"• **Trend:** EMA20 {'above' if analysis['entry_bullish'] else 'below'} EMA50\n"
        f"• **Macro Bias:** {'🟢 Bullish' if analysis['macro_bullish'] else '🔴 Bearish'}"
    )

    chart_buf = await asyncio.to_thread(
        generate_chart_snapshot,
        analysis["df_entry"],
        f"XAUUSD Real-Time Chart ({tf_lbl})",
        60,
        analysis.get("near_zone"),
        analysis.get("macro_fvg"),
    )

    if chart_buf:
        await update.effective_message.reply_photo(
            photo=chart_buf,
            caption=caption,
            parse_mode="Markdown"
        )
    else:
        await update.effective_message.reply_text(caption, parse_mode="Markdown")


@admin_only
async def market_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now_utc = datetime.now(timezone.utc)
    ch = now_utc.hour

    # Session UTC Hours (Approximate standard market hours)
    sydney = (22 <= ch or ch < 7)
    tokyo = (0 <= ch < 9)
    london = (8 <= ch < 17)
    ny = (13 <= ch < 22)

    # Volatility / Overlap flags
    london_ny_overlap = london and ny
    asian_session = tokyo or sydney

    reply = (
        f"🕒 **GLOBAL MARKET SESSIONS (UTC: {now_utc.strftime('%H:%M')})**\n\n"
        f"🇦🇺 **Sydney:** {'OPEN 🟢' if sydney else 'CLOSED 🔴'}\n"
        f"🇯🇵 **Tokyo (Asian):** {'OPEN 🟢' if tokyo else 'CLOSED 🔴'}\n"
        f"🇬🇧 **London:** {'OPEN 🟢' if london else 'CLOSED 🔴'}\n"
        f"🇺🇸 **New York:** {'OPEN 🟢' if ny else 'CLOSED 🔴'}\n\n"
        f"{'⚡ **HIGH VOLATILITY OVERLAP! (London + NY)**' if london_ny_overlap else ''}"
        f"{'😴 **ASIAN CONSOLIDATION PHASE (Low Volatility for Gold)**' if asian_session and not (london or ny) else ''}"
    )
    await update.message.reply_text(reply, parse_mode="Markdown")


@admin_only
async def diagnose_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = await asyncio.to_thread(get_gold_symbol)
    analysis = await asyncio.to_thread(analyze_market, symbol) if symbol else None
    if not analysis:
        await update.message.reply_text("❌ Diagnostic failed: Unable to fetch MT5 market analysis.")
        return

    entry_lbl = TF_LABELS.get(ALERT_STATE["entry_tf"], "Entry")
    trend_lbl = TF_LABELS.get(ALERT_STATE["trend_tf"], "Trend")
    macro_lbl = TF_LABELS.get(ALERT_STATE["macro_tf"], "Macro")

    ob = analysis.get("order_block", {})
    ob_status = "None"
    if ob.get("bullish_ob"):
        ob_status = f"Bullish OB @ ${ob.get('ob_level')}"
    elif ob.get("bearish_ob"):
        ob_status = f"Bearish OB @ ${ob.get('ob_level')}"

    msg = (
        "🔍 **LIVE TRIGGER DIAGNOSTIC**\n\n"
        f"📍 **Price:** `${analysis['close_price']}` | **14-ATR:** `${analysis['atr_val']}`\n"
        f"📈 **RSI:** `{analysis['rsi_val']}` (Buy <= `{ALERT_STATE['rsi_buy_threshold']}`, Sell >= `{ALERT_STATE['rsi_sell_threshold']}`)\n\n"
        f"• **{entry_lbl} EMA:** {'🟢 Bull' if analysis['entry_bullish'] else '🔴 Bear'}\n"
        f"• **{trend_lbl} EMA:** {'🟢 Bull' if analysis['trend_bullish'] else '🔴 Bear'}\n"
        f"• **{macro_lbl} EMA:** {'🟢 Bull' if analysis['macro_bullish'] else '🔴 Bear'}\n\n"
        f"💧 **Liquidity Sweep:** Bullish={analysis['sweeps']['bullish_sweep']}, Bearish={analysis['sweeps']['bearish_sweep']}\n"
        f"⚡ **Fair Value Gap:** Bullish={analysis['fvg']['bullish_fvg']}, Bearish={analysis['fvg']['bearish_fvg']}\n"
        f"🧱 **Order Block:** {ob_status}\n"
        f"🎯 **Nearest S/R:** {analysis['near_zone']['type'].title() if analysis['near_zone'] else 'None'} @ ${analysis['near_zone']['price'] if analysis['near_zone'] else 'N/A'}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")