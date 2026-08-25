import MetaTrader5 as mt5
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import ContextTypes
from config import ALERT_STATE, TIMEFRAME_PRESETS, CONFLUENCE_WEIGHTS
from database import get_signal_stats
from mt5_engine import get_gold_symbol, fetch_candles
from news_engine import fetch_economic_events
from strategy.evaluator import analyze_market
from bot.jobs import build_status_snapshot

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤵‍♂️ **Trading Butler Online (Modular Edition)**\n\nUse `/status`, `/stats`, or `/diagnose` to monitor your bot.", parse_mode="Markdown")

async def diagnose_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = get_gold_symbol()
    analysis = analyze_market(symbol) if symbol else None
    if not analysis:
        await update.message.reply_text("❌ Diagnostic failed.")
        return

    msg = (
        "🔍 **LIVE TRIGGER DIAGNOSTIC**\n\n"
        f"📍 **Price:** `${analysis['close_price']}` | **14-ATR:** `${analysis['atr_val']}`\n"
        rf"📈 **RSI:** `{analysis['rsi_val']}` (Buy $\le {ALERT_STATE['rsi_buy_threshold']}$, Sell $\ge {ALERT_STATE['rsi_sell_threshold']}$)\n\n"
        f"• **5M EMA:** {'🟢 Bull' if analysis['entry_bullish'] else '🔴 Bear'}\n"
        f"• **1H EMA:** {'🟢 Bull' if analysis['trend_bullish'] else '🔴 Bear'}\n"
        f"• **4H EMA:** {'🟢 Bull' if analysis['macro_bullish'] else '🔴 Bear'}\n\n"
        f"💧 **Liquidity Sweep:** Bullish={analysis['sweeps']['bullish_sweep']}, Bearish={analysis['sweeps']['bearish_sweep']}\n"
        f"⚡ **Fair Value Gap:** Bullish={analysis['fvg']['bullish_fvg']}, Bearish={analysis['fvg']['bearish_fvg']}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    counts = get_signal_stats()
    pending = counts.get('PENDING', 0)
    tp1 = counts.get('HIT_TP1', 0)
    tp2 = counts.get('HIT_TP2', 0)
    sl = counts.get('HIT_SL', 0)
    total_closed = tp1 + tp2 + sl
    win_rate = round(((tp1 + tp2) / total_closed * 100), 1) if total_closed > 0 else 0.0

    msg = (
        "📊 **FORWARD-TESTING PERFORMANCE STATS**\n\n"
        f"• **Total Signals Logged:** `{sum(counts.values())}`\n"
        f"• **Active/Pending:** `{pending}`\n"
        f"• **Hit TP1:** `{tp1}` 🎯\n"
        f"• **Hit TP2:** `{tp2}` 🚀\n"
        f"• **Hit Stop Loss:** `{sl}` 🛡️\n\n"
        f"📈 **Win Rate (TP1/TP2):** `{win_rate}%`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    snapshot = build_status_snapshot()
    await update.message.reply_text(f"🩺 **BOT STATUS CHECK**\n\n{snapshot}", parse_mode="Markdown")