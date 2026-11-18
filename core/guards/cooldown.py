# core/guards/cooldown.py
import asyncio

from core.database import get_sl_cooldown_remaining_s
from core.guards import guard


@guard("sl_cooldown", scope="signal", order=10)
async def check(ctx):
    remaining = await asyncio.to_thread(get_sl_cooldown_remaining_s)
    return f"{remaining / 60:.0f}m left after the last stop-loss" if remaining > 0 else None