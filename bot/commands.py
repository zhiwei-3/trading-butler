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
        await update.message.reply_text(
            "⚠️ **Usage:** `/set <key> <value>`\n\n"
            "**Supported Settings:**\n"
            "• `/set buy_rsi 80` — Buy RSI threshold (30 standard, 80 for testing)\n"
            "• `/set sell_rsi 20` — Sell RSI threshold (70 standard, 20 for testing)\n"
            "• `/set score 50` — Minimum confluence score\n"
            "• `/set rrr 1.5` — Minimum Risk-to-Reward Ratio\n"
            "• `/set risk 1.0` — Risk percentage per trade",
            parse_mode="Markdown"
        )
        return

    key = context.args[0].lower()
    val_str = context.args[1]

    try:
        val = float(val_str)
        if key in ("buy_rsi", "rsi_buy", "rsi_buy_threshold"):
            ALERT_STATE["rsi_buy_threshold"] = val
            setting_name = "rsi_buy_threshold"
        elif key in ("sell_rsi", "rsi_sell", "rsi_sell_threshold"):
            ALERT_STATE["rsi_sell_threshold"] = val
            setting_name = "rsi_sell_threshold"
        elif key in ("score", "min_score", "min_confluence_score"):
            ALERT_STATE["min_confluence_score"] = val
            setting_name = "min_confluence_score"
        elif key in ("rrr", "min_rrr"):
            ALERT_STATE["min_rrr"] = val
            setting_name = "min_rrr"
        elif key in ("risk", "risk_percent", "risk_pct"):
            ALERT_STATE["risk_percent"] = val
            setting_name = "risk_percent"
        else:
            await update.message.reply_text(f"❌ Unknown setting key `{key}`.", parse_mode="Markdown")
            return

        save_settings()
        await update.message.reply_text(
            f"✅ **Setting Updated**\n\n`{setting_name}` set to `{ALERT_STATE[setting_name]}`.",
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

    msg = (
        f"🧪 **BACKTEST RESULTS — {result['mode'].upper()}** ({result['days']}d)\n\n"
        f"• **Trades:** `{result['total_trades']}` | **Wins:** `{result['wins']}` | **Losses:** `{result['losses']}` | **Open:** `{result['open']}`\n"
        f"• **Win Rate:** `{result['win_rate']}%`\n"
        f"• **Avg R / Trade:** `{result['avg_r']}R` | **Net R:** `{result['net_r']}R`\n"
        f"• **Max Drawdown:** `{result['max_drawdown_r']}R`\n"
        f"• **Filters:** Min Score `{result['min_confluence_score']}/100`, Min RRR `1:{result['min_rrr']}`\n\n"
        f"📊 **Top Confluence Factors (win rate when present):**\n{factor_lines}\n\n"
        f"⚠️ *Simulated on historical bars with a synthetic spread — real fills, slippage, and news gaps will vary. "
        f"Hypothetical/backtested results are not indicative of future performance.*"
    )

    chart_buf = generate_equity_chart(result["equity_curve"], title=f"XAUUSD Backtest Equity — {result['mode'].upper()} ({result['days']}d)")
    if chart_buf:
        await update.message.reply_photo(photo=chart_buf, caption=msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

async def optimize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    days = 30
    mode = None

    if args:
        try:
            days = int(args[0])
        except ValueError:
            await update.message.reply_text("⚠️ **Usage:** `/optimize <days> [scalp|intraday|swing]`", parse_mode="Markdown")
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
    progress_msg = await update.message.reply_text(
        f"⏳ Running optimization sweep — `{days}d` on `{label}`... `0%`\n"
        f"_(this fires ~25 backtests, may take a while)_",
        parse_mode="Markdown"
    )

    loop = asyncio.get_running_loop()

    def progress_callback(pct):
        async def _edit():
            try:
                await progress_msg.edit_text(
                    f"⏳ Running optimization sweep — `{days}d` on `{label}`... `{pct}%`",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        asyncio.run_coroutine_threadsafe(_edit(), loop)

    try:
        result = await asyncio.to_thread(run_backtest_sweep, symbol, days, mode, progress_callback=progress_callback)
    except Exception as e:
        logging.exception("Optimization sweep crashed")
        await update.message.reply_text(f"❌ **Sweep crashed:** `{e}`", parse_mode="Markdown")
        return

    grid = result["grid"]
    valid = [g for g in grid if "error" not in g]
    if not valid:
        first_error = grid[0].get("error", "Unknown error") if grid else "No results returned."
        await update.message.reply_text(f"❌ **Sweep failed:** {first_error}", parse_mode="Markdown")
        return

    valid_sorted = sorted(valid, key=lambda g: g["net_r"], reverse=True)

    lines = [f"🧪 **OPTIMIZATION SWEEP — {label.upper()}** ({days}d)\n", "`RRR  Score  Trades  Win%   NetR    MaxDD`"]
    for g in valid:
        lines.append(
            f"`1:{g['min_rrr']:<4}{g['min_confluence_score']:<7}{g['total_trades']:<8}"
            f"{g['win_rate']:<7}{g['net_r']:<8}{g['max_drawdown_r']}`"
        )

    lines.append("\n🏆 **Top 3 by Net R:**")
    for g in valid_sorted[:3]:
        trades_per_day = round(g['total_trades'] / days, 2)
        lines.append(
            f"  • RRR `1:{g['min_rrr']}`, Score `{g['min_confluence_score']}` → "
            f"`{g['total_trades']}` trades (`{trades_per_day}/day`), `{g['win_rate']}%` win, `{g['net_r']}R` net, "
            f"`{g['max_drawdown_r']}R` max DD"
        )

    lines.append(
        "\n⚠️ *Backtested on historical bars with a synthetic spread — real fills, slippage, "
        "and news gaps will vary. More trades/day is not the goal by itself; weigh it against "
        "win rate, net R, and max drawdown together. Hypothetical results are not indicative of "
        "future performance.*"
    )

    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:3900] + "\n\n... (truncated — grid too large for one message)"

    await update.message.reply_text(msg, parse_mode="Markdown")