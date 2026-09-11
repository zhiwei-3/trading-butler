import logging
import asyncio
import MetaTrader5 as mt5
import pandas_ta as ta
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, TIMEFRAME_PRESETS, STRATEGY_PRESETS, CONFLUENCE_WEIGHTS, save_settings
from database import get_signal_stats
from mt5_engine import get_gold_symbol, fetch_candles, calculate_position_size
from news_engine import fetch_economic_events
from strategy.backtester import run_backtest, generate_equity_chart, run_backtest_sweep
from strategy.evaluator import analyze_market
from strategy.chart import generate_chart_snapshot
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
        "• `/diagnose` - Live Signal Diagnostic Check\n"
        "• `/backtest <days> [mode]` - Replay Strategy Over Historical Data\n\n"
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

    events = await fetch_economic_events(impact_level=impact, currency="USD")
    
    # Handle API Network Drop
    if events is None:
        await update.message.reply_text(
            "📡 **Network Error:** Unable to reach economic calendar server. Please try again in a few moments.", 
            parse_mode="Markdown"
        )
        return

    # Handle Actual 0 Events Case
    if len(events) == 0:
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
        if sl_pips <= 0:
            await update.message.reply_text("❌ Stop loss pips must be greater than 0.", parse_mode="Markdown")
            return

        symbol = get_gold_symbol() or "XAUUSD"
        
        # Convert pips to absolute price distance (Assuming XAUUSD 1 pip = 0.1 price move)
        # Adjust the multiplier if your broker formats gold points differently
        sl_dist = sl_pips * 0.1 
        
        # Route through the exact same calculator used by live signals
        pos = calculate_position_size(symbol, sl_dist, risk_pct)

        reply = (
            f"🧮 **POSITION RISK CALCULATOR ({symbol})**\n\n"
            f"• **Account Balance:** `${pos['balance']:,.2f}`\n"
            f"• **Risk Target ({risk_pct}%):** `${pos['risk_usd']:,.2f}`\n"
            f"• **Stop Loss Distance:** `{sl_pips} pips`\n\n"
            f"🎯 **Recommended Lot Size:** `{pos['lots']}` Lots"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid numerical values.")
    except Exception as e:
        await update.message.reply_text(f"❌ Calculation error: {e}")

async def gold_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /gold and /snapshot commands by returning live metrics with an annotated chart photo."""
    symbol = get_gold_symbol() or "XAUUSD"
    
    await update.message.reply_chat_action("upload_photo")
    analysis = analyze_market(symbol)

    if not analysis:
        await update.message.reply_text("❌ Failed to fetch MT5 market data for Gold.")
        return

    close_p = analysis["close_price"]
    rsi_p = analysis["rsi_val"]
    atr_p = analysis["atr_val"]
    tf_lbl = analysis["tf_label"]
    struct = analysis["structure"]

    # Caption text summary
    caption = (
        f"📊 **XAUUSD REAL-TIME MARKET SNAPSHOT**\n\n"
        f"• **Timeframe:** `{tf_lbl}` | **Price:** `${close_p:.2f}`\n"
        f"• **RSI (14):** `{rsi_p:.1f}` | **ATR (14):** `${atr_p:.2f}`\n"
        f"• **Structure:** `{struct}`\n"
        f"• **Trend:** EMA20 {'above' if analysis['entry_bullish'] else 'below'} EMA50\n"
        f"• **Macro Bias:** {'🟢 Bullish' if analysis['macro_bullish'] else '🔴 Bearish'}"
    )

    # Generate real-time candlestick chart image
    chart_buf = generate_chart_snapshot(
        df=analysis["df_entry"],
        title=f"XAUUSD Real-Time Chart ({tf_lbl})",
        near_zone=analysis.get("near_zone"),
        macro_fvg=analysis.get("macro_fvg")
    )

    if chart_buf:
        await update.message.reply_photo(
            photo=chart_buf,
            caption=caption,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(caption, parse_mode="Markdown")

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

async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dynamically updates bot settings in ALERT_STATE."""
    if not context.args or len(context.args) < 2:
        # Show all current configurations if no arguments are provided
        reply = (
            "⚙️ **DYNAMIC SETTINGS MANAGER**\n\n"
            f"• **Buy RSI (`buy_rsi`):** `{ALERT_STATE.get('rsi_buy_threshold', 30)}`\n"
            f"• **Sell RSI (`sell_rsi`):** `{ALERT_STATE.get('rsi_sell_threshold', 70)}`\n"
            f"• **Min Score (`score`):** `{ALERT_STATE.get('min_confluence_score', 50)}/100`\n"
            f"• **Min RRR (`rrr`):** `1:{ALERT_STATE.get('min_rrr', 1.3)}`\n"
            f"• **Risk % (`risk`):** `{ALERT_STATE.get('risk_percent', 1.0)}%`\n"
            f"• **SL ATR Mult (`sl_mult`):** `{ALERT_STATE.get('sl_atr_mult', 1.5)}x`\n"
            f"• **TP1 ATR Mult (`tp1_mult`):** `{ALERT_STATE.get('tp1_atr_mult', 1.0)}x`\n"
            f"• **TP2 ATR Mult (`tp2_mult`):** `{ALERT_STATE.get('tp2_atr_mult', 2.0)}x`\n"
            f"• **Max Spread (`spread`):** `{ALERT_STATE.get('max_allowed_spread_pips', 30)} pips`\n\n"
            "**Usage:** `/set <key> <value>` (e.g., `/set tp1_mult 1.5`)"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")
        return

    key = context.args[0].lower()
    val_str = context.args[1]

    try:
        val = float(val_str)
        setting_name = ""

        # Map shorthand keys to actual dictionary keys
        if key in ("buy_rsi", "rsi_buy", "rsi_buy_threshold"):
            setting_name = "rsi_buy_threshold"
        elif key in ("sell_rsi", "rsi_sell", "rsi_sell_threshold"):
            setting_name = "rsi_sell_threshold"
        elif key in ("score", "min_score", "min_confluence_score"):
            setting_name = "min_confluence_score"
            val = int(val)  # Force integer for scores
        elif key in ("rrr", "min_rrr"):
            setting_name = "min_rrr"
        elif key in ("risk", "risk_percent", "risk_pct"):
            setting_name = "risk_percent"
        elif key in ("sl_mult", "sl_atr"):
            setting_name = "sl_atr_mult"
        elif key in ("tp1_mult", "tp1_atr"):
            setting_name = "tp1_atr_mult"
        elif key in ("tp2_mult", "tp2_atr"):
            setting_name = "tp2_atr_mult"
        elif key in ("spread", "max_spread"):
            setting_name = "max_allowed_spread_pips"
        else:
            await update.message.reply_text(f"❌ Unknown setting key `{key}`.", parse_mode="Markdown")
            return

        # Grab old value for the confirmation message
        old_val = ALERT_STATE.get(setting_name, "N/A")
        
        # Apply and save
        ALERT_STATE[setting_name] = val
        save_settings()
        
        # Clean up the name (e.g., 'risk_percent' -> 'Risk Percent')
        # This prevents Markdown errors caused by unescaped underscores
        display_name = setting_name.replace("_", " ").title()
        
        await update.message.reply_text(
            f"✅ **Setting Updated**\n\n"
            f"**{display_name}** modified:\n"
            f"`{old_val}` ➡️ `{val}`",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid numeric value.", parse_mode="Markdown")

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

async def set_strategy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    current = ALERT_STATE.get("active_strategy", "smc_confluence")

    if not args or args[0].lower() not in STRATEGY_PRESETS:
        lines = "\n".join(f"• `{k}` — {v}" for k, v in STRATEGY_PRESETS.items())
        await update.message.reply_text(
            f"⚙️ **ACTIVE STRATEGY:** `{current.upper()}`\n\n"
            f"**Available Presets:**\n{lines}\n\n"
            f"**Usage:** `/strategy smc_confluence` or `/strategy ema_cross`",
            parse_mode="Markdown"
        )
        return

    mode = args[0].lower()
    ALERT_STATE["active_strategy"] = mode
    save_settings()

    strategy_label = STRATEGY_PRESETS[mode]
    await update.message.reply_text(
        f"✅ Active strategy updated to `{mode.upper()}`\n"
        f"📝 `{strategy_label}`",
        parse_mode="Markdown"
    )
    
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
    elif sub == "rsi_margin" and len(args) >= 2:
        try:
            ALERT_STATE["watch_rsi_margin"] = float(args[1])
        except ValueError:
            await update.message.reply_text("❌ Provide a numeric RSI margin.", parse_mode="Markdown")
            return
    else:
        await update.message.reply_text("⚠️ Unknown option. Use `on`, `off`, or `rsi_margin <val>`.", parse_mode="Markdown")
        return
    save_settings()
    await update.message.reply_text("✅ Watchlist setting updated.", parse_mode="Markdown")

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
    else:
        await update.message.reply_text("⚠️ **Unknown option.** Usage: `/heartbeat on|off|test`", parse_mode="Markdown")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    snapshot = build_status_snapshot()
    await update.message.reply_text(f"🩺 **BOT STATUS CHECK**\n\n{snapshot}", parse_mode="Markdown")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    counts = get_signal_stats()
    pending = counts.get('PENDING', 0)
    tp1 = counts.get('HIT_TP1', 0)
    tp2 = counts.get('HIT_TP2', 0)
    be = counts.get('CLOSED_BE', 0)
    sl = counts.get('HIT_SL', 0)

    # Any trade reaching TP1, TP2, or closing at Break-Even is a win
    wins = tp1 + tp2 + be
    total_closed = wins + sl
    win_rate = round((wins / total_closed * 100), 1) if total_closed > 0 else 0.0

    reply = (
        f"📊 **FORWARD-TESTING PERFORMANCE STATS**\n\n"
        f"• **Total Signals:** `{sum(counts.values())}` | **Pending:** `{pending}`\n"
        f"• **TP1 Active Runners:** `{tp1}` 🎯\n"
        f"• **TP2 Hits:** `{tp2}` 🚀\n"
        f"• **Break-Even Closes:** `{be}` 🔒\n"
        f"• **Stop Loss Hits:** `{sl}` 🛡️\n\n"
        f"📈 **Win Rate:** `{win_rate}%` (`{wins}/{total_closed}` closed trades)"
    )
    await update.message.reply_text(reply, parse_mode="Markdown")

async def diagnose_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = get_gold_symbol()
    analysis = analyze_market(symbol) if symbol else None
    if not analysis:
        await update.message.reply_text("❌ Diagnostic failed: Unable to fetch MT5 market analysis.")
        return

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
        f"• **5M EMA:** {'🟢 Bull' if analysis['entry_bullish'] else '🔴 Bear'}\n"
        f"• **15M EMA:** {'🟢 Bull' if analysis['trend_bullish'] else '🔴 Bear'}\n"
        f"• **1H EMA:** {'🟢 Bull' if analysis['macro_bullish'] else '🔴 Bear'}\n\n"
        f"💧 **Liquidity Sweep:** Bullish={analysis['sweeps']['bullish_sweep']}, Bearish={analysis['sweeps']['bearish_sweep']}\n"
        f"⚡ **Fair Value Gap:** Bullish={analysis['fvg']['bullish_fvg']}, Bearish={analysis['fvg']['bearish_fvg']}\n"
        f"🧱 **Order Block:** {ob_status}\n"
        f"🎯 **Nearest S/R:** {analysis['near_zone']['type'].title() if analysis['near_zone'] else 'None'} @ ${analysis['near_zone']['price'] if analysis['near_zone'] else 'N/A'}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    days = 30
    mode = None

    if args:
        try:
            days = int(args[0])
        except ValueError:
            await update.message.reply_text("⚠️ **Usage:** `/backtest <days> [scalp|intraday|swing]`", parse_mode="Markdown")
            return
        if len(args) >= 2:
            mode = args[1].lower()
            if mode not in TIMEFRAME_PRESETS:
                await update.message.reply_text("⚠️ Invalid mode. Use `scalp`, `intraday`, or `swing`.", parse_mode="Markdown")
                return

    days = max(1, min(days, 180))

    symbol = get_gold_symbol()
    if not symbol:
        await update.message.reply_text("❌ MT5 Gold symbol not found.")
        return

    label = mode or ALERT_STATE['timeframe_mode']
    progress_msg = await update.message.reply_text(f"⏳ Running backtest — `{days}d` on `{label}`... `0%`", parse_mode="Markdown")

    loop = asyncio.get_running_loop()

    def progress_callback(pct):
        async def _edit():
            try:
                await progress_msg.edit_text(f"⏳ Running backtest — `{days}d` on `{label}`... `{pct}%`", parse_mode="Markdown")
            except Exception:
                pass  # ignore harmless "message not modified" / edit rate-limit errors
        asyncio.run_coroutine_threadsafe(_edit(), loop)

    try:
        result = await asyncio.to_thread(run_backtest, symbol, days, mode, progress_callback=progress_callback)
    except Exception as e:
        logging.exception("Backtest crashed")
        await update.message.reply_text(f"❌ **Backtest crashed:** `{e}`\n\nCheck the console log for the full traceback.", parse_mode="Markdown")
        return

    if "error" in result:
        await update.message.reply_text(f"❌ **Backtest failed:** {result['error']}", parse_mode="Markdown")
        return

    if result["total_trades"] == 0:
        await update.message.reply_text(
            f"📭 **No signals fired** over the last `{result['days']}d` on `{result['mode']}` "
            f"(min score `{result['min_confluence_score']}`, min RRR `1:{result['min_rrr']}`).",
            parse_mode="Markdown"
        )
        return

    factor_lines = "\n".join(
        f"  • {label}: `{wr}%` win rate ({n} occurrences)"
        for label, wr, n in result["factor_summary"][:6]
    ) or "  • Not enough closed trades yet for a factor breakdown."

    # Fetch active configs for the readout
    strat = ALERT_STATE.get("active_strategy", "smc_confluence").replace("_", " ").upper()
    rsi_buy = ALERT_STATE.get("rsi_buy_threshold", 30)
    rsi_sell = ALERT_STATE.get("rsi_sell_threshold", 70)
    sl_mult = ALERT_STATE.get("sl_atr_mult", 1.5)
    tp1_mult = ALERT_STATE.get("tp1_atr_mult", 1.0)
    tp2_mult = ALERT_STATE.get("tp2_atr_mult", 2.0)
    struct_gate = "ON ✅" if ALERT_STATE.get("require_structure_break") else "OFF ❌"
    vol_gate = "ON ✅" if ALERT_STATE.get("require_volume_atr_filter") else "OFF ❌"

    msg = (
        f"🧪 **BACKTEST RESULTS — {result['mode'].upper()}** ({result['days']}d)\n"
        f"⚙️ **Strategy:** `{strat}`\n\n"
        f"**--- Parameters Used ---**\n"
        f"• **RSI Thresholds:** Buy `<= {rsi_buy}` | Sell `>= {rsi_sell}`\n"
        f"• **ATR Multipliers:** SL `{sl_mult}x` | TP1 `{tp1_mult}x` | TP2 `{tp2_mult}x`\n"
        f"• **Score & RRR:** Min Score `{result['min_confluence_score']}/100` | Min RRR `1:{result['min_rrr']}`\n"
        f"• **Hard Gates:** Structure: {struct_gate} | Vol/ATR: {vol_gate}\n\n"
        f"**--- Performance ---**\n"
        f"• **Trades:** `{result['total_trades']}` | **Wins:** `{result['wins']}` | **Losses:** `{result['losses']}` | **Open:** `{result['open']}`\n"
        f"• **Win Rate:** `{result['win_rate']}%`\n"
        f"• **Avg R / Trade:** `{result['avg_r']}R` | **Net R:** `{result['net_r']}R`\n"
        f"• **Max Drawdown:** `{result['max_drawdown_r']}R`\n\n"
        f"📊 **Top Confluence Factors:**\n{factor_lines}\n\n"
        f"⚠️ *Simulated on historical bars with a synthetic spread — real fills, slippage, and news gaps will vary.*"
    )

    chart_buf = generate_equity_chart(result["equity_curve"], title=f"XAUUSD Backtest Equity — {result['mode'].upper()} ({result['days']}d)")
    if chart_buf:
        await update.message.reply_photo(photo=chart_buf, caption=msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

import time
import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

async def optimize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    days = 30
    mode = None
    flags = set()

    # 1. Flexible Argument Parser
    if args:
        remaining_args = list(args)
        
        # Check if first argument is a number (days)
        if remaining_args[0].isdigit():
            days = int(remaining_args.pop(0))
        
        # Extract flags and timeframe mode from remaining arguments
        unprocessed = []
        for arg in remaining_args:
            arg_lower = arg.lower()
            if arg_lower in ("strategies", "sl", "rsi"):
                flags.add(arg_lower)
            elif arg_lower in TIMEFRAME_PRESETS:
                mode = arg_lower
            else:
                unprocessed.append(arg)

        if unprocessed:
            await update.message.reply_text(
                f"⚠️ **Unknown argument(s):** `{', '.join(unprocessed)}`\n\n"
                "**Usage:** `/optimize [days] [scalp|intraday|swing] [strategies] [sl] [rsi]`\n\n"
                "**Examples:**\n"
                "• `/optimize scalp`\n"
                "• `/optimize 14 intraday strategies`\n"
                "• `/optimize swing sl rsi`",
                parse_mode="Markdown"
            )
            return

    days = max(1, min(days, 180))

    if len(flags) > 2:
        await update.message.reply_text(
            "⚠️ Combine at most 2 of `strategies` / `sl` / `rsi` at once to keep the grid size manageable.",
            parse_mode="Markdown"
        )
        return

    symbol = get_gold_symbol()
    if not symbol:
        await update.message.reply_text("❌ MT5 Gold symbol not found.")
        return

    label = mode or ALERT_STATE.get('timeframe_mode', 'scalp')

    # Base grid shrinks as more axes are added to prevent exponential grid explosion
    if not flags:
        rrr_values, score_values = [1.3, 1.5, 2.0, 2.5, 3.0], [20, 25, 30, 35, 40]
    else:
        rrr_values, score_values = [1.5, 2.0, 3.0], [25, 35]

    strategy_values = list(STRATEGY_PRESETS.keys()) if "strategies" in flags else None
    sl_values = [1.2, 1.5, 1.7, 2.0, 2.5] if "sl" in flags else None
    rsi_pairs = [(30, 60), (35, 60), (40, 60), (30, 65)] if "rsi" in flags else None

    flag_str = f" [{', '.join(sorted(flags))}]" if flags else ""
    progress_msg = await update.message.reply_text(
        f"⏳ Running optimization sweep — `{days}d` on `{label.upper()}`{flag_str}... `0%`",
        parse_mode="Markdown"
    )

    loop = asyncio.get_running_loop()
    last_edit_time = [0.0]
    last_pct = [-1]

    # 2. Throttled Progress Callback (Prevents Telegram API Rate-Limit Errors)
    def progress_callback(pct):
        now = time.time()
        # Only issue API edit if pct changed AND at least 1.5s passed (or completed at 100%)
        if pct != last_pct[0] and (now - last_edit_time[0] >= 1.5 or pct == 100):
            last_edit_time[0] = now
            last_pct[0] = pct
            
            async def _edit():
                try:
                    await progress_msg.edit_text(
                        f"⏳ Running optimization sweep — `{days}d` on `{label.upper()}`{flag_str}... `{pct}%`",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
            asyncio.run_coroutine_threadsafe(_edit(), loop)

    start_time = time.perf_counter()

    try:
        result = await asyncio.to_thread(
            run_backtest_sweep, symbol, days, mode,
            rrr_values=rrr_values, score_values=score_values,
            strategy_values=strategy_values, sl_values=sl_values, rsi_pairs=rsi_pairs,
            progress_callback=progress_callback
        )
    except Exception as e:
        logging.exception("Optimization sweep crashed")
        await update.message.reply_text(f"❌ **Sweep crashed:** `{e}`", parse_mode="Markdown")
        return

    elapsed = round(time.perf_counter() - start_time, 1)

    if "error" in result:
        await update.message.reply_text(f"❌ **Sweep rejected:** {result['error']}", parse_mode="Markdown")
        return

    grid = result.get("grid", [])
    valid = [g for g in grid if "error" not in g]
    if not valid:
        first_error = grid[0].get("error", "No valid parameter combinations produced signals.") if grid else "No results returned."
        await update.message.reply_text(f"❌ **Sweep failed:** {first_error}", parse_mode="Markdown")
        return

    valid_sorted = sorted(valid, key=lambda g: g["net_r"], reverse=True)

    def describe(g):
        parts = [f"RRR `1:{g['min_rrr']}`", f"Score `{g['min_confluence_score']}`"]
        if result.get("swept_strategies") and "strategy" in g:
            strat_display = g['strategy'].replace('_', ' ').title()
            parts.insert(0, f"`{strat_display}`")
        if result.get("swept_sl") and "sl_atr_mult" in g:
            parts.append(f"SL `{g['sl_atr_mult']}x`")
        if result.get("swept_rsi") and "rsi_pair" in g:
            parts.append(f"RSI `{g['rsi_pair'][0]}/{g['rsi_pair'][1]}`")
        return " ".join(parts)

    lines = [f"🧪 **OPTIMIZATION SWEEP — {label.upper()}** ({days}d | ⚡ `{elapsed}s`)\n"]

    top_count = 8 if flags else 5
    lines.append(f"🏆 **Top {top_count} combinations by Net R:**")

    for g in valid_sorted[:top_count]:
        trades_per_day = round(g['total_trades'] / days, 2)
        lines.append(
            f"  • {describe(g)} ➡️ `{g['total_trades']}` trades (`{trades_per_day}/d`), "
            f"`{g['win_rate']}%` win, `{g['net_r']}R` net, `{g['max_drawdown_r']}R` max DD"
        )

    lines.append(
        "\n⚠️ *Backtested on historical bars with synthetic spread. Sweeping multiple dimensions increases overfit risk — validate on out-of-sample data before live deployment.*"
    )

    # 3. Line-by-Line Safe Character Truncation
    final_lines = []
    current_len = 0
    for line in lines:
        if current_len + len(line) + 1 > 3800:
            final_lines.append("\n... *(results truncated for length)*")
            break
        final_lines.append(line)
        current_len += len(line) + 1

    await update.message.reply_text("\n".join(final_lines), parse_mode="Markdown")