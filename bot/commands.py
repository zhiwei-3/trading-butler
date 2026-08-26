import MetaTrader5 as mt5
import pandas_ta as ta
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, TIMEFRAME_PRESETS, CONFLUENCE_WEIGHTS, save_settings
from database import get_signal_stats
from mt5_engine import get_gold_symbol, fetch_candles
from news_engine import fetch_economic_events
from strategy.evaluator import analyze_market
from bot.jobs import (
    build_status_snapshot,
    market_scanner_job,
    ensure_watchdog_running,
    restart_heartbeat_job,
)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_watchdog_running(context.job_queue, chat_id)
    if not ALERT_STATE["heartbeat_chat_id"]:
        restart_heartbeat_job(context.job_queue, chat_id)
        save_settings()
    await update.message.reply_text(
        "🤵‍♂️ **Trading Butler Online (Full Stack Modular)**\n\n"
        "• `/scanner_on` / `/scanner_off` - Toggle Market Scanner\n"
        "• `/news` - High-impact USD Economic Calendar\n"
        "• `/spread` - Live Bid/Ask & Spread Guard Status\n"
        "• `/gold` - Gold Technical Snapshot\n"
        "• `/calc <bal> <risk%> <sl>` - Position Size Calculator\n"
        "• `/session` - Market Session Clock\n"
        "• `/timeframe <scalp|intraday|swing>` - Switch Strategy Timeframes\n"
        "• `/filters` - View/Adjust Structure & Volume Filters\n"
        "• `/confluence <0-100>` - View/Adjust Min Signal Confidence Score\n"
        "• `/watchlist` - View/Adjust 'Setup Forming' Early Heads-Up Pings\n"
        "• `/status` - Full Health Check\n"
        "• `/stats` - Forward-Testing Performance & Win Rate\n"
        "• `/heartbeat` - View/Adjust Periodic Pings\n"
        "• `/diagnose` - Live Signal Diagnostic Check\n\n"
        "💓 24/7 monitoring is active in this chat.",
        parse_mode="Markdown"
    )

async def enable_scanner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ALERT_STATE["scanner_enabled"] = True
    save_settings()
    current_jobs = context.job_queue.get_jobs_by_name("xauusd_scanner")
    for job in current_jobs:
        job.schedule_removal()

    context.job_queue.run_repeating(market_scanner_job, interval=60, first=5, chat_id=chat_id, name="xauusd_scanner")
    ensure_watchdog_running(context.job_queue, chat_id)
    if not ALERT_STATE["heartbeat_chat_id"]:
        restart_heartbeat_job(context.job_queue, chat_id)
    await update.message.reply_text("🟢 **Market Scanner Activated!** Checking XAUUSD every 60 seconds.", parse_mode="Markdown")

async def disable_scanner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ALERT_STATE["scanner_enabled"] = False
    save_settings()
    current_jobs = context.job_queue.get_jobs_by_name("xauusd_scanner")
    for job in current_jobs:
        job.schedule_removal()
    await update.message.reply_text("🔴 **Market Scanner Deactivated.**", parse_mode="Markdown")

async def news_calendar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    impact = args[0].strip().lower() if args else "high"
    
    valid_impacts = ["high", "medium", "low", "holiday", "all"]
    if impact not in valid_impacts:
        await update.message.reply_text(
            "⚠️ **Usage:** `/news <high|medium|low|holiday|all>`", 
            parse_mode="Markdown"
        )
        return

    events = fetch_economic_events(impact_level=impact, currency="USD")
    if not events:
        await update.message.reply_text(
            f"🟢 **No `{impact.upper()}` impact USD events found for this week.**", 
            parse_mode="Markdown"
        )
        return

    impact_emojis = {
        "high": "🔴",
        "medium": "🟠",
        "low": "🟡",
        "holiday": "⚪"
    }
    
    events_by_date = {}
    for ev in events:
        raw_date = ev.get("date", "")
        try:
            dt_utc = datetime.fromisoformat(raw_date).astimezone(timezone.utc)
            date_key = dt_utc.strftime("%A, %b %d")
            time_str = dt_utc.strftime("%H:%M UTC")
        except (ValueError, TypeError):
            date_key, time_str = "Upcoming Events", "N/A"
            
        ev["formatted_time"] = time_str
        events_by_date.setdefault(date_key, []).append(ev)

    msg = f"🗓️ **WEEKLY USD ECONOMIC CALENDAR ({impact.upper()} IMPACT)**\n\n"
    for date_header, day_events in events_by_date.items():
        msg += f"📅 **{date_header}**\n"
        for ev in day_events:
            title = ev.get("title", "N/A")
            time_str = ev.get("formatted_time", "N/A")
            ev_imp = str(ev.get("impact", "")).strip().lower()
            badge = impact_emojis.get(ev_imp, "⚪")
            forecast, prev = ev.get("forecast", ""), ev.get("previous", "")
            extra = f" (FC: {forecast} | Prev: {prev})" if forecast or prev else ""
            msg += f"  {badge} `{time_str}` — {title}{extra}\n"
        msg += "\n"

    await update.message.reply_text(msg, parse_mode="Markdown")

async def spread_check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = get_gold_symbol()
    if not symbol:
        await update.message.reply_text("❌ MT5 Gold symbol not found.")
        return
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        await update.message.reply_text("❌ Unable to fetch live tick data.")
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
    await update.message.reply_text(reply, parse_mode="Markdown")

async def calc_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) < 3:
            await update.message.reply_text("⚠️ **Usage:** `/calc <balance> <risk_pct> <sl_pips>`", parse_mode="Markdown")
            return
        balance, risk_pct, sl_pips = float(args[0]), float(args[1]), float(args[2])
        risk_amount = balance * (risk_pct / 100.0)

        pip_value_per_lot = 10.0
        lot_size = round(risk_amount / (sl_pips * pip_value_per_lot), 2)
        reply = (
            f"🧮 **POSITION RISK CALCULATOR (XAUUSD)**\n\n"
            f"• **Balance:** `${balance:,.2f}`\n"
            f"• **Risk Target ({risk_pct}%):** `${risk_amount:,.2f}`\n"
            f"• **Stop Loss:** `{sl_pips} pips` (${sl_pips/10:.2f} move)\n\n"
            f"🎯 **Recommended Lot Size:** `{max(lot_size, 0.01)}` Lots"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid numerical values.")

async def gold_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = get_gold_symbol()
    if not symbol:
        await update.message.reply_text("❌ MT5 Gold symbol not found.")
        return
    m15_rates = fetch_candles(symbol, mt5.TIMEFRAME_M15, 100)
    d1_rates = fetch_candles(symbol, mt5.TIMEFRAME_D1, 2)
    if m15_rates is None or d1_rates is None:
        await update.message.reply_text("❌ Failed to read MT5 chart data.")
        return

    today_open = d1_rates['open'].iloc[-1]
    tick = mt5.symbol_info_tick(symbol)
    current_price = tick.ask if tick else m15_rates['close'].iloc[-1]
    daily_change = round(((current_price - today_open) / today_open) * 100, 2)
    m15_rates['ATR'] = ta.atr(m15_rates['high'], m15_rates['low'], m15_rates['close'], length=14)
    latest_atr = round(m15_rates['ATR'].iloc[-1], 2)

    reply = (
        f"🪙 **XAUUSD LIVE SNAPSHOT**\n\n"
        f"• **Current Price:** `${current_price:,.2f}`\n"
        f"• **24h Change:** `{daily_change}%`\n"
        f"• **Volatility (14-ATR):** `${latest_atr}` pips\n"
        f"• **Scanner Status:** `{'ENABLED 🟢' if ALERT_STATE['scanner_enabled'] else 'DISABLED 🔴'}`"
    )
    await update.message.reply_text(reply, parse_mode="Markdown")

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

async def set_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or args[0].lower() not in TIMEFRAME_PRESETS:
        preset_lines = "\n".join(f"• `{k}` → {v['label']}" for k, v in TIMEFRAME_PRESETS.items())
        await update.message.reply_text(f"⚠️ **Usage:** `/timeframe <mode>`\n\n{preset_lines}", parse_mode="Markdown")
        return
    mode = args[0].lower()
    preset = TIMEFRAME_PRESETS[mode]
    ALERT_STATE["timeframe_mode"] = mode
    ALERT_STATE["entry_tf"] = preset["entry"]
    ALERT_STATE["trend_tf"] = preset["trend"]
    ALERT_STATE["macro_tf"] = preset["macro"]
    ALERT_STATE["last_rsi_signal"] = None
    save_settings()
    await update.message.reply_text(f"✅ Timeframe set to `{mode.upper()}` ({preset['label']})", parse_mode="Markdown")

async def filters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        reply = (
            "🎛️ **STRATEGY FILTERS**\n\n"
            f"• **Structure (BOS):** {'ON ✅' if ALERT_STATE['require_structure_break'] else 'OFF ❌'}\n"
            f"• **Volume/ATR Filter:** {'ON ✅' if ALERT_STATE['require_volume_atr_filter'] else 'OFF ❌'}\n\n"
            "**Usage:** `/filters structure on|off` or `/filters volume on|off`"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")
        return

    if len(args) >= 2:
        key, value = args[0].lower(), args[1].lower()
        if key == "structure" and value in ("on", "off"):
            ALERT_STATE["require_structure_break"] = (value == "on")
            save_settings()
            await update.message.reply_text(f"✅ Structure filter **{value.upper()}**", parse_mode="Markdown")
            return
        elif key == "volume" and value in ("on", "off"):
            ALERT_STATE["require_volume_atr_filter"] = (value == "on")
            save_settings()
            await update.message.reply_text(f"✅ Volume/ATR filter **{value.upper()}**", parse_mode="Markdown")
            return
    await update.message.reply_text("⚠️ Invalid format. Send `/filters` alone to see usage.", parse_mode="Markdown")

async def confluence_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        weight_lines = "\n".join(f"  • {k.replace('_', ' ').title()}: `{v} pts`" for k, v in CONFLUENCE_WEIGHTS.items())
        await update.message.reply_text(f"🎯 **CONFLUENCE SCORING**\n\nMinimum score: `{ALERT_STATE['min_confluence_score']}/100`\n\n{weight_lines}\n\n**Usage:** `/confluence <0-100>`", parse_mode="Markdown")
        return
    try:
        val = int(args[0])
        if 0 <= val <= 100:
            ALERT_STATE["min_confluence_score"] = val
            save_settings()
            await update.message.reply_text(f"✅ Minimum confluence score set to `{val}/100`", parse_mode="Markdown")
        else: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Provide a number between 0 and 100.")

async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "👀 **WATCHLIST SETTINGS**\n\n"
            f"• **Status:** {'ON ✅' if ALERT_STATE['setup_forming_enabled'] else 'OFF ❌'}\n"
            f"• **RSI Margin:** `{ALERT_STATE['watch_rsi_margin']}` pts\n"
            f"• **Score Margin:** `{ALERT_STATE['watch_score_margin']}` pts\n\n"
            "**Usage:** `/watchlist on|off` or `/watchlist rsi_margin <val>`",
            parse_mode="Markdown"
        )
        return
    sub = args[0].lower()
    if sub == "on": ALERT_STATE["setup_forming_enabled"] = True
    elif sub == "off": ALERT_STATE["setup_forming_enabled"] = False
    elif sub == "rsi_margin" and len(args) >= 2: ALERT_STATE["watch_rsi_margin"] = float(args[1])
    save_settings()
    await update.message.reply_text(f"✅ Watchlist setting updated.", parse_mode="Markdown")

async def heartbeat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    chat_id = update.effective_chat.id
    ensure_watchdog_running(context.job_queue, chat_id)
    if not args:
        status = "🟢 ON" if ALERT_STATE["heartbeat_enabled"] else "🔴 OFF"
        await update.message.reply_text(f"💓 **HEARTBEAT:** {status} every `{ALERT_STATE['heartbeat_interval_hours']}h`\n\n**Usage:** `/heartbeat on|off|test`", parse_mode="Markdown")
        return
    sub = args[0].lower()
    if sub == "test":
        snapshot = build_status_snapshot()
        await update.message.reply_text(f"💓 **TEST HEARTBEAT**\n\n{snapshot}", parse_mode="Markdown")
    elif sub == "on":
        ALERT_STATE["heartbeat_enabled"] = True
        restart_heartbeat_job(context.job_queue, chat_id)
        save_settings()
        await update.message.reply_text("✅ Heartbeat **ON**", parse_mode="Markdown")
    elif sub == "off":
        ALERT_STATE["heartbeat_enabled"] = False
        save_settings()
        await update.message.reply_text("🔴 Heartbeat **OFF**", parse_mode="Markdown")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    snapshot = build_status_snapshot()
    await update.message.reply_text(f"🩺 **BOT STATUS CHECK**\n\n{snapshot}", parse_mode="Markdown")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    counts = get_signal_stats()
    pending = counts.get('PENDING', 0)
    tp1, tp2, sl = counts.get('HIT_TP1', 0), counts.get('HIT_TP2', 0), counts.get('HIT_SL', 0)
    total_closed = tp1 + tp2 + sl
    win_rate = round(((tp1 + tp2) / total_closed * 100), 1) if total_closed > 0 else 0.0
    reply = (
        f"📊 **FORWARD-TESTING PERFORMANCE STATS**\n\n"
        f"• **Total Signals:** `{sum(counts.values())}` | **Pending:** `{pending}`\n"
        f"• **TP1:** `{tp1}` 🎯 | **TP2:** `{tp2}` 🚀 | **SL:** `{sl}` 🛡️\n"
        f"📈 **Win Rate:** `{win_rate}%`"
    )
    await update.message.reply_text(reply, parse_mode="Markdown")

async def diagnose_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = get_gold_symbol()
    analysis = analyze_market(symbol) if symbol else None
    if not analysis:
        await update.message.reply_text("❌ Diagnostic failed.")
        return

    msg = (
        "🔍 **LIVE TRIGGER DIAGNOSTIC**\n\n"
        f"📍 **Price:** `${analysis['close_price']}` | **14-ATR:** `${analysis['atr_val']}`\n"
        f"📈 **RSI:** `{analysis['rsi_val']}` (Buy ≤ `{ALERT_STATE['rsi_buy_threshold']}`, Sell ≥ `{ALERT_STATE['rsi_sell_threshold']}`)\n\n"
        f"• **5M EMA:** {'🟢 Bull' if analysis['entry_bullish'] else '🔴 Bear'}\n"
        f"• **1H EMA:** {'🟢 Bull' if analysis['trend_bullish'] else '🔴 Bear'}\n"
        f"• **4H EMA:** {'🟢 Bull' if analysis['macro_bullish'] else '🔴 Bear'}\n\n"
        f"💧 **Liquidity Sweep:** Bullish={analysis['sweeps']['bullish_sweep']}, Bearish={analysis['sweeps']['bearish_sweep']}\n"
        f"⚡ **Fair Value Gap:** Bullish={analysis['fvg']['bullish_fvg']}, Bearish={analysis['fvg']['bearish_fvg']}\n"
        f"🧱 **Order Block:** Bullish={analysis['order_block']['bullish_ob']}, Bearish={analysis['order_block']['bearish_ob']}"
        + (f" @ `${analysis['order_block']['ob_level']}`" if analysis['order_block']['ob_level'] is not None else "") + "\n"
        f"📐 **Min RRR Required:** `1:{ALERT_STATE['min_rrr']}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")