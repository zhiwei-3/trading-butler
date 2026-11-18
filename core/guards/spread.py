# core/guards/spread.py
import asyncio

from config import ALERT_STATE
from core.guards import guard
from core.mt5_engine import get_tick


def pips_of_spread(tick):
    """XAUUSD: 1 pip = 0.10."""
    return round((tick.ask - tick.bid) * 10, 1)


@guard("spread", scope="scan", order=20)
async def check(ctx):
    tick = await asyncio.to_thread(get_tick, ctx.symbol)
    if not tick:
        return None          # can't judge without a tick; the MT5 watchdog reports outages
    pips = pips_of_spread(tick)
    limit = ALERT_STATE["max_allowed_spread_pips"]
    return f"spread {pips} pips > limit {limit}" if pips > limit else None