import os
import json
from datetime import datetime, timezone
import MetaTrader5 as mt5
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
USER_ID = os.getenv("TELEGRAM_USER_ID", None)
DB_FILE = "trading_butler.db"
SETTINGS_FILE = "settings.json"

BOT_START_TIME = datetime.now(timezone.utc)

TIMEFRAME_PRESETS = {
    "scalp":    {"entry": mt5.TIMEFRAME_M5,  "trend": mt5.TIMEFRAME_M15, "macro": mt5.TIMEFRAME_H1, "label": "5M Entry / 15M Trend / 1H Macro (Scalping)"},
    "intraday": {"entry": mt5.TIMEFRAME_M15, "trend": mt5.TIMEFRAME_H1,  "macro": mt5.TIMEFRAME_H4, "label": "15M Entry / 1H Trend / 4H Macro (Intraday)"},
    "swing":    {"entry": mt5.TIMEFRAME_H1,  "trend": mt5.TIMEFRAME_H4,  "macro": mt5.TIMEFRAME_D1, "label": "1H Entry / 4H Trend / Daily Macro (Swing)"},
}

# BUGFIX: used by /diagnose and the timeframe-selector menu so displayed labels
# always match the ACTUAL active preset instead of being hardcoded strings that
# drift out of sync with TIMEFRAME_PRESETS.
TF_LABELS = {
    mt5.TIMEFRAME_M1: "1M", mt5.TIMEFRAME_M5: "5M", mt5.TIMEFRAME_M15: "15M",
    mt5.TIMEFRAME_M30: "30M", mt5.TIMEFRAME_H1: "1H", mt5.TIMEFRAME_H4: "4H",
    mt5.TIMEFRAME_D1: "1D",
}

STRATEGY_PRESETS = {
    "smc_confluence": "Smart Money Concepts + Multi-TF Confluence (Default)",
    "htf_fvg_sweep": "HTF (1H) FVG Tap + LTF (5M) Sweep & Shift Model",
    "smc_displacement": "SMC Displacement & FVG Expansion Strategy",
    "ema_cross": "Triple EMA Trend Crossover + Volatility Filter",
    "rsi_reversion": "RSI Overbought/Oversold S/R Zone Reversion",
}

ALERT_STATE = {
    "last_rsi_signal": None,
    "scanner_enabled": True,
    "news_lockout": False,
    # BUGFIX: was a set() — sets aren't prunable-by-age without extra bookkeeping
    # and this dict lets news_engine.py evict stale keys instead of growing forever.
    "news_warned_events": {},
    "max_allowed_spread_pips": 10.0,

    "timeframe_mode": "scalp",
    "entry_tf": TIMEFRAME_PRESETS["scalp"]["entry"],
    "trend_tf": TIMEFRAME_PRESETS["scalp"]["trend"],
    "macro_tf": TIMEFRAME_PRESETS["scalp"]["macro"],
    "active_strategy": "smc_confluence",

    "require_structure_break": False,
    "swing_lookback": 60,
    "fractal_window": 2,

    "require_volume_atr_filter": False,
    "atr_multiplier": 1.0,
    "volume_multiplier": 1.2,
    "vol_atr_avg_period": 20,

    "rsi_buy_threshold": 30.0,
    "rsi_sell_threshold": 70.0,

    "sl_atr_mult": 1.2,
    "tp1_atr_mult": 2.0,
    "tp2_atr_mult": 3.5,
    "min_rrr": 1.5,  # minimum TP1:SL reward-to-risk ratio required to fire a signal
    "risk_percent": 1.0,  # Default 1% risk per trade

    "min_confluence_score": 35,
    "sr_lookback": 180,
    "sr_cluster_pct": 0.0015,
    "sr_min_touches": 2,
    "sr_max_distance_pct": 0.004,

    "setup_forming_enabled": True,
    "last_watch_signal": None,
    "watch_rsi_margin": 5,
    "watch_score_margin": 10,

    "circuit_breaker_enabled": True,        # Master ON/OFF toggle
    "max_daily_losses": 3,                  # Max consecutive losses allowed per day
    "max_daily_drawdown_r": 3.0,            # Max daily R-multiple drawdown allowed (-3.0R)

    "news_blockade_enabled": True,             # Master ON/OFF toggle
    "news_blockade_impacts": ["high"],         # Levels to block: ["high"], ["high", "medium"], etc.
    "news_blockade_mins_before": 30,           # Pause scanner N minutes BEFORE news release
    "news_blockade_mins_after": 15,            # Keep scanner paused N minutes AFTER news release

    "heartbeat_enabled": True,
    "heartbeat_interval_hours": 1,
    "last_heartbeat_at": None,
    "mt5_connected": True,
    "consecutive_mt5_failures": 0,

    # === LIVE TRADE EXECUTION ===
    "auto_trade_enabled": False,        # master switch — bot places orders
    "trade_dry_run": True,              # build+log the request, never send it
    "magic_number": 770077,             # bot only ever touches its own positions
    "max_slippage_points": 30,          # 'deviation' passed to order_send
    # Dual-entry uses 2 tickets per signal (one TP1-only, one TP2-runner), so this
    # must be >=2 for dual-entry signals to actually get their second leg — but the
    # second leg of a signal's OWN pair bypasses this cap (see trade_engine.py); it
    # only limits how many *separate* signals can be open at once.
    "max_open_positions": 2,
    "max_daily_trades": 5,              # hard cap on live fills (tickets) per UTC day
    "max_entry_drift_pct": 25.0,        # abort if price drifted >25% of SL dist since signal
    "fixed_lot_size": 0.01,             # flat lot size per leg — NOT scaled by risk_percent
    "dual_entry_score_threshold": 50,   # smc_confluence score >= this -> open 2 legs (TP1 + TP2/BE runner)
    "flatten_on_circuit_breaker": True,
    "trade_comment": "TradingButler",
}

CONFLUENCE_WEIGHTS = {
    "rsi_zone": 15,
    "ema_trend": 10,
    "macro_trend": 10,
    "structure_bos": 10,
    "liquidity_sweep": 10,
    "fvg": 10,
    "volume_atr": 10,
    "macd": 10,
    "candlestick": 5,
    "divergence": 5,
    "sr_zone": 5,
}

PERSISTENT_KEYS = [
    "scanner_enabled",
    "max_allowed_spread_pips",
    "timeframe_mode",
    "active_strategy",
    "require_structure_break",
    "require_volume_atr_filter",
    "atr_multiplier",
    "volume_multiplier",
    "rsi_buy_threshold",
    "rsi_sell_threshold",
    "sl_atr_mult",
    "tp1_atr_mult",
    "tp2_atr_mult",
    "min_rrr",
    "risk_percent",
    "min_confluence_score",
    "setup_forming_enabled",
    "watch_rsi_margin",
    "watch_score_margin",
    "heartbeat_enabled",
    "heartbeat_interval_hours",
    # --- live execution ---
    "auto_trade_enabled",
    "trade_dry_run",
    "magic_number",
    "max_slippage_points",
    "max_open_positions",
    "max_daily_trades",
    "max_entry_drift_pct",
    "fixed_lot_size",
    "dual_entry_score_threshold",
    "flatten_on_circuit_breaker",
    "trade_comment",
]

def save_settings():
    """Saves current configurable parameters to settings.json."""
    data = {k: ALERT_STATE[k] for k in PERSISTENT_KEYS if k in ALERT_STATE}
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"⚠️ Failed to save settings: {e}")

def load_settings():
    """Loads saved settings from settings.json on startup."""
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
        for k, v in data.items():
            if k in ALERT_STATE:
                ALERT_STATE[k] = v

        mode = ALERT_STATE.get("timeframe_mode", "scalp")
        if mode in TIMEFRAME_PRESETS:
            ALERT_STATE["entry_tf"] = TIMEFRAME_PRESETS[mode]["entry"]
            ALERT_STATE["trend_tf"] = TIMEFRAME_PRESETS[mode]["trend"]
            ALERT_STATE["macro_tf"] = TIMEFRAME_PRESETS[mode]["macro"]

        # BUGFIX: a bot restarted while a real position was open must never resume
        # sending live orders silently — force back to dry-run and make the
        # operator re-confirm with /trade live CONFIRM.
        if ALERT_STATE.get("auto_trade_enabled") and not ALERT_STATE.get("trade_dry_run", True):
            ALERT_STATE["trade_dry_run"] = True
            print("⚠️ Live execution reset to DRY-RUN on startup. Re-arm with /trade live CONFIRM.")

        print("✅ Loaded persistent settings from settings.json")
    except Exception as e:
        print(f"⚠️ Failed to load settings: {e}")

load_settings()
