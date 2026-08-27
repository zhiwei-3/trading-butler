import os
import json
from datetime import datetime, timezone
import MetaTrader5 as mt5
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
YOUR_CHAT_ID = os.getenv("CHAT_ID", None)
DB_FILE = "trading_butler.db"
SETTINGS_FILE = "settings.json"

BOT_START_TIME = datetime.now(timezone.utc)

TIMEFRAME_PRESETS = {
    "scalp":    {"entry": mt5.TIMEFRAME_M5,  "trend": mt5.TIMEFRAME_M15, "macro": mt5.TIMEFRAME_H1, "label": "5M Entry / 15M Trend / 1H Macro (Scalping)"},
    "intraday": {"entry": mt5.TIMEFRAME_M15, "trend": mt5.TIMEFRAME_H1,  "macro": mt5.TIMEFRAME_H4, "label": "15M Entry / 1H Trend / 4H Macro (Intraday)"},
    "swing":    {"entry": mt5.TIMEFRAME_H1,  "trend": mt5.TIMEFRAME_H4,  "macro": mt5.TIMEFRAME_D1, "label": "1H Entry / 4H Trend / Daily Macro (Swing)"},
}

ALERT_STATE = {
    "last_rsi_signal": None,
    "scanner_enabled": True,
    "news_lockout": False,
    "news_warned_events": set(),
    "max_allowed_spread_pips": 30.0,

    "timeframe_mode": "scalp",
    "entry_tf": TIMEFRAME_PRESETS["scalp"]["entry"],
    "trend_tf": TIMEFRAME_PRESETS["scalp"]["trend"],
    "macro_tf": TIMEFRAME_PRESETS["scalp"]["macro"],

    "require_structure_break": False,
    "swing_lookback": 60,
    "fractal_window": 2,

    "require_volume_atr_filter": False,
    "atr_multiplier": 1.0,
    "volume_multiplier": 1.2,
    "vol_atr_avg_period": 20,

    "rsi_buy_threshold": 40.0,
    "rsi_sell_threshold": 60.0,

    "sl_atr_mult": 1.7,
    "tp1_atr_mult": 2.0,
    "tp2_atr_mult": 3.5,
    "min_rrr": 1.3,  # minimum TP1:SL reward-to-risk ratio required to fire a signal

    "min_confluence_score": 35,
    "sr_lookback": 180,
    "sr_cluster_pct": 0.0015,
    "sr_min_touches": 2,
    "sr_max_distance_pct": 0.004,

    "setup_forming_enabled": True,
    "last_watch_signal": None,
    "watch_rsi_margin": 10,
    "watch_score_margin": 25,

    "heartbeat_enabled": True,
    "heartbeat_interval_hours": 1,
    "heartbeat_chat_id": None,
    "last_heartbeat_at": None,
    "mt5_connected": True,
    "consecutive_mt5_failures": 0,
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
    "min_confluence_score",
    "setup_forming_enabled",
    "watch_rsi_margin",
    "watch_score_margin",
    "heartbeat_enabled",
    "heartbeat_interval_hours",
    "heartbeat_chat_id",
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
        print("✅ Loaded persistent settings from settings.json")
    except Exception as e:
        print(f"⚠️ Failed to load settings: {e}")

load_settings()