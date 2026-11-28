# bot/jobs/status.py
import asyncio
from datetime import datetime, timezone, timedelta

from config import ALERT_STATE, BOT_START_TIME
from strategies import active_strategies, get_params, sizing_summary, gates_summary
from core.db import get_sl_cooldown_remaining_s
from core.mt5 import check_mt5_alive
from core.execution import list_managed_positions
from bot.messaging import md_safe
from core.guards import active_blocks

def format_uptime(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours or days: parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


async def build_status_snapshot() -> str:
    uptime = format_uptime(datetime.now(timezone.utc) - BOT_START_TIME)
    mt5_ok = await asyncio.to_thread(check_mt5_alive)
    ALERT_STATE["mt5_connected"] = mt5_ok
    last_hb = ALERT_STATE["last_heartbeat_at"]
    last_hb_str = last_hb.astimezone().strftime("%Y-%m-%d %H:%M %Z") if last_hb else "Not sent yet"

    trade_mode = "🔴 OFF"
    if ALERT_STATE.get("auto_trade_enabled"):
        trade_mode = "🧪 DRY-RUN" if ALERT_STATE.get("trade_dry_run", True) else "🟢 LIVE"
    try:
        open_managed = len(await asyncio.to_thread(list_managed_positions))
    except Exception:
        open_managed = 0

    cd_s = await asyncio.to_thread(get_sl_cooldown_remaining_s)
    cooldown_str = f"⏸️ {cd_s / 60:.0f}m left" if cd_s > 0 else "Clear"

    active = active_strategies()
    strat_line = " + ".join(f"`{s.key.upper()}`" for s in active) or "`NONE (outside schedule)`"
    schedule = ALERT_STATE.get("strategy_schedule", "off")

    blocks = active_blocks()
    guards_line = "Clear" if not blocks else " · ".join(f"🔒 {n}: {md_safe(r)}" for n, r in blocks.items())

    return (
        f"• **Uptime:** `{uptime}`\n"
        f"• **MT5 Connection:** {'🟢 Connected' if mt5_ok else '🔴 Disconnected'}\n"
        f"• **Scanner:** {'🟢 Running' if ALERT_STATE['scanner_enabled'] else '🔴 Stopped'}\n"
        f"• **Auto-Trade:** {trade_mode} | **Open Positions:** `{open_managed}`\n"
        f"• **Active Now:** {strat_line}\n"
        f"• **Schedule (UTC):** `{schedule}`\n"
        f"• **Sizing:** `{sizing_summary(active[0].key if active else None)}`\n"
        f"• **Timeframe Mode:** `{ALERT_STATE['timeframe_mode']}`\n"
        f"• **Hard Gates:** {' · '.join(f'{s.key}: {gates_summary(get_params(s.key))}' for s in active) or '—'}\n"
        f"• **Volume/ATR Filter:** {'ON' if ALERT_STATE['require_volume_atr_filter'] else 'OFF'}\n"
        f"• **Guards:** {guards_line}\n"
        f"• **SL Cooldown:** {cooldown_str}\n"
        f"• **Heartbeat Pings:** {'🟢 ON' if ALERT_STATE['heartbeat_enabled'] else '🔴 OFF'} every `{ALERT_STATE['heartbeat_interval_hours']}h`\n"
        f"• **Last Heartbeat Sent:** `{last_hb_str}`"
    )