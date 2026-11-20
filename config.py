import os
import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from dotenv import load_dotenv

from utils.parsers import num, boolean, choice, choice_list

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
USER_ID = os.getenv("TELEGRAM_USER_ID", None)
DB_FILE = "trading_butler.db"
SETTINGS_FILE = "settings.json"

BOT_START_TIME = datetime.now(timezone.utc)


# ---------------------------------------------------------------- timeframes
# Plain names everywhere ("M5", "H1"); core.mt5 resolves them to terminal constants at the edge.

TIMEFRAME_PRESETS = {
    "scalp":    {"entry": "M5",  "trend": "M15", "macro": "H1", "label": "5M Entry / 15M Trend / 1H Macro (Scalping)"},
    "intraday": {"entry": "M15", "trend": "H1",  "macro": "H4", "label": "15M Entry / 1H Trend / 4H Macro (Intraday)"},
    "swing":    {"entry": "H1",  "trend": "H4",  "macro": "D1", "label": "1H Entry / 4H Trend / Daily Macro (Swing)"},
}

TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}

# Display labels (charts, /gold, /diagnose, menus) keyed by timeframe name.
TF_LABELS = {"M1": "1M", "M5": "5M", "M15": "15M", "M30": "30M", "H1": "1H", "H4": "4H", "D1": "1D"}


# ---------------------------------------------------------------- settings schema

@dataclass(frozen=True)
class Setting:
    """One core setting. Declared once; defaults, persistence and /set are all derived from it."""
    key: str
    default: Any
    parse: Optional[Callable[[str], Any]] = None   # str -> value, raises ValueError. Also validates the saved value on load.
    aliases: tuple = ()                            # /set names (the key itself is always accepted too). Empty = not settable.
    label: str = ""                                # shown in /set
    group: str = "Other"                           # /set section
    unit: str = ""                                 # appended when displayed, e.g. " pips"
    persist: bool = True                           # saved to settings.json

    @property
    def settable(self):
        return bool(self.aliases and self.parse)


SCHEMA = (
    # ---- scanner / active selections (changed by commands and the menu, not /set)
    Setting("scanner_enabled", True, boolean),
    Setting("timeframe_mode", "scalp", choice(*TIMEFRAME_PRESETS)),
    Setting("active_strategy", "none"),
    Setting("setup_forming_enabled", True, boolean),
    Setting("broker_utc_offset_hours", None),
    Setting("heartbeat_enabled", False, boolean),
    Setting("heartbeat_interval_hours", 1, num(0.01, 168)),
    Setting("max_allowed_spread_pips", 10.0, num(0, 500, lo_open=True), ("spread", "max_spread"),
            "Max Spread", "Scanner", " pips"),

    # ---- shared analysis gates (computed by the core pipeline for every strategy; /filters toggles them)
    Setting("require_structure_break", False, boolean),
    Setting("require_volume_atr_filter", False, boolean),
    Setting("atr_multiplier", 1.0, num(0, 20, lo_open=True)),
    Setting("volume_multiplier", 1.2, num(0, 20, lo_open=True)),

    # ---- analysis constants (fixed, not persisted)
    Setting("fractal_window", 2, persist=False),
    Setting("sr_lookback", 180, persist=False),
    Setting("sr_cluster_pct", 0.0015, persist=False),
    Setting("sr_min_touches", 2, persist=False),
    Setting("sr_max_distance_pct", 0.004, persist=False),

    # ---- strategy-owned (the strategy registry reads/writes these)
    Setting("strategy_params", {}),
    Setting("_legacy", {}),             # retired global values found in settings.json, used once to seed strategy params

    # ---- trade execution (see /trade)
    Setting("auto_trade_enabled", False, boolean),            # safe default: disarmed
    Setting("trade_dry_run", True, boolean),                  # safe default: orders are built but never sent
    Setting("fixed_lot_size", 0.01, num(0, 10, lo_open=True), ("lot", "fixed_lot"),
            "Fixed Lot / Leg", "Trade Execution"),
    Setting("magic_number", 770077, num(1, 2**31 - 1, integer=True), ("magic",),
            "Magic Number", "Trade Execution"),
    Setting("max_slippage_points", 30, num(0, 1000, integer=True), ("slippage", "deviation"),
            "Slippage", "Trade Execution", " pts"),
    Setting("max_daily_trades", 5, num(1, 100, integer=True), ("max_trades",),
            "Max Fills / Day", "Trade Execution"),
    Setting("max_entry_drift_pct", 25.0, num(0, 500, lo_open=True), ("drift", "max_drift"),
            "Entry Drift Abort", "Trade Execution", "% of SL"),
    Setting("flatten_on_circuit_breaker", True, boolean, ("flatten_cb",),
            "Flatten on Breaker", "Trade Execution"),
    Setting("trade_comment", "TradingButler"),

    # ---- circuit breaker
    Setting("circuit_breaker_enabled", True, boolean, ("cb_enabled", "circuit_breaker"),
            "Status", "Circuit Breaker"),
    Setting("max_daily_losses", 3, num(1, 100, integer=True), ("cb_losses", "max_losses"),
            "Max Consecutive Losses", "Circuit Breaker"),
    Setting("max_daily_drawdown_r", 3.0, num(0, 100, lo_open=True), ("cb_drawdown", "max_drawdown"),
            "Max Daily Drawdown", "Circuit Breaker", "R"),

    # ---- SL cooldown
    Setting("sl_cooldown_enabled", True, boolean, ("sl_cooldown", "cooldown"),
            "Status", "SL Cooldown"),
    Setting("sl_cooldown_minutes", 30, num(0, 1440, integer=True), ("cooldown_mins", "sl_cooldown_mins"),
            "Minutes", "SL Cooldown", "m"),

    # ---- news blockade
    Setting("news_blockade_enabled", True, boolean, ("news_blockade", "news_toggle"),
            "Blockade", "News Blockade"),
    Setting("news_blockade_mins_before", 30, num(0, 1440, integer=True), ("news_before", "news_mins_before"),
            "Mins Before", "News Blockade", "m"),
    Setting("news_blockade_mins_after", 30, num(0, 1440, integer=True), ("news_after", "news_mins_after"),
            "Mins After", "News Blockade", "m"),
    Setting("news_blockade_impacts", ["high"], choice_list("high", "medium", "low", "holiday"),
            ("news_impact", "news_level", "news_impacts"), "Impact Levels", "News Blockade"),
)

# In-memory only: never saved, never settable.
RUNTIME_STATE = {
    "news_warned_events": {},       # event_key -> warned_at (UTC); pruned by age in news_engine
    "entry_tf": None, "trend_tf": None, "macro_tf": None,       # derived from timeframe_mode
    "last_heartbeat_at": None,
    "mt5_connected": True,
    "consecutive_mt5_failures": 0,
}

ALERT_STATE = {**{s.key: copy.deepcopy(s.default) for s in SCHEMA}, **copy.deepcopy(RUNTIME_STATE)}
PERSISTENT_KEYS = tuple(s.key for s in SCHEMA if s.persist)

_BY_KEY = {s.key: s for s in SCHEMA}
_BY_ALIAS = {}
for _s in SCHEMA:
    if _s.settable:
        for _name in (_s.key, *_s.aliases):
            if _name in _BY_ALIAS:
                raise ValueError(f"Duplicate /set name '{_name}' in settings schema")
            _BY_ALIAS[_name] = _s


def find_setting(token):
    """Setting by /set name (key or alias); None if unknown or not settable."""
    return _BY_ALIAS.get(str(token).lower().strip())


def settable_groups():
    """{group: [Setting, ...]} for /set, in schema order."""
    out = {}
    for s in SCHEMA:
        if s.settable:
            out.setdefault(s.group, []).append(s)
    return out


def to_text(value):
    """Value -> the string form Setting.parse expects."""
    return ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)


def apply_timeframe_mode(mode):
    """Single place that switches the active timeframe preset. Returns False for an unknown mode."""
    preset = TIMEFRAME_PRESETS.get(mode)
    if preset is None:
        return False
    ALERT_STATE.update(timeframe_mode=mode, entry_tf=preset["entry"],
                       trend_tf=preset["trend"], macro_tf=preset["macro"])
    return True


# ---------------------------------------------------------------- persistence

def save_settings():
    """Saves current configurable parameters to settings.json."""
    data = {k: ALERT_STATE[k] for k in PERSISTENT_KEYS if k in ALERT_STATE}
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"⚠️ Failed to save settings: {e}")


def _coerce(spec, value):
    """Validates a saved value through the setting's own parser; a bad value falls back to the default."""
    if spec.parse is None:
        return value
    try:
        return spec.parse(to_text(value))
    except (ValueError, TypeError):
        print(f"⚠️ settings.json: invalid value {value!r} for '{spec.key}' — using default {spec.default!r}.")
        return copy.deepcopy(spec.default)


def load_settings():
    """Loads saved settings from settings.json on startup."""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
            legacy = {}
            for k, v in data.items():
                spec = _BY_KEY.get(k)
                if spec is None:
                    legacy[k] = v            # retired global keys (rsi_*, sl_atr_mult, tb_*, ...)
                else:
                    ALERT_STATE[k] = _coerce(spec, v)
            # Kept so tuned values survive until a strategy that owns them is installed.
            ALERT_STATE["_legacy"] = {**ALERT_STATE.get("_legacy", {}), **legacy}
            print("✅ Loaded persistent settings from settings.json")
        except Exception as e:
            print(f"⚠️ Failed to load settings: {e}")

    apply_timeframe_mode(ALERT_STATE["timeframe_mode"]) or apply_timeframe_mode("scalp")
    if ALERT_STATE.get("auto_trade_enabled") and not ALERT_STATE.get("trade_dry_run", True):
        print("🟢 Live execution is ARMED on startup (restored from settings.json).")


load_settings()