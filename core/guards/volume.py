# core/guards/volume.py
from config import ALERT_STATE
from core.guards import guard


@guard("volume", scope="signal", order=20)
async def check(ctx):
    if not ALERT_STATE.get("require_volume_atr_filter", False):
        return None
    if ctx.analysis is not None and not ctx.analysis.get("vol_filter_ok", True):
        return "ATR / tick volume below their recent average"
    return None