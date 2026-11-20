# core/account.py
"""MT5 account snapshot: balance/equity/margin, open positions and the deal history behind /account."""

from collections import defaultdict
from datetime import datetime, timezone, timedelta

from config import ALERT_STATE
from core.mt5 import mt5, MT5_LOCK, check_mt5_alive, get_broker_utc_offset_seconds

HISTORY_START = datetime(2000, 1, 1, tzinfo=timezone.utc)


def fetch_account_data():
    """BLOCKING — call via asyncio.to_thread. Returns (data, error). Exactly one of them is None."""
    if not check_mt5_alive():
        return None, "🔴 MT5 terminal is not connected or unreachable."

    offset_s = int(get_broker_utc_offset_seconds())
    shift = timedelta(seconds=offset_s)
    now = datetime.now(timezone.utc)
    magic = int(ALERT_STATE.get("magic_number", 770077))

    with MT5_LOCK:
        info = mt5.account_info()
        if info is None:
            return None, "❌ Failed to retrieve MT5 account details."
        deals = mt5.history_deals_get(HISTORY_START + shift, now + shift + timedelta(days=1))
        positions = mt5.positions_get()

    trades, deposits, withdrawals = [], 0.0, 0.0
    # Commission/fee is often charged on the OPENING deal. Carry it onto the closing
    # deal so per-trade P&L matches /journal (which sums every deal of a position).
    entry_cost = defaultdict(float)
    for d in (deals or []):
        if d.type == mt5.DEAL_TYPE_BALANCE:
            if d.profit > 0:
                deposits += d.profit
            else:
                withdrawals += -d.profit
            continue
        if d.type not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
            continue
        cost = d.commission + getattr(d, "fee", 0.0)
        if d.entry == mt5.DEAL_ENTRY_IN:
            entry_cost[d.position_id] += cost
        elif d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT, mt5.DEAL_ENTRY_OUT_BY):
            trades.append({
                "ticket": d.ticket,
                "time": datetime.fromtimestamp(d.time - offset_s, tz=timezone.utc).astimezone(),
                "symbol": d.symbol,
                "type": "SELL" if d.type == mt5.DEAL_TYPE_BUY else "BUY",
                "volume": d.volume,
                "price": d.price,
                "net_pnl": d.profit + d.swap + cost + entry_cost.pop(d.position_id, 0.0),
                "is_bot": d.magic == magic,
            })
    trades.sort(key=lambda t: t["time"])

    open_all = list(positions or [])
    open_bot = [p for p in open_all if p.magic == magic]

    return {
        "login": info.login,
        "server": info.server,
        "currency": info.currency or "USD",
        "leverage": info.leverage,
        "balance": info.balance,
        "equity": info.equity,
        "profit": info.profit,
        "margin": info.margin,
        "free_margin": info.margin_free,
        "margin_level": info.margin_level if info.margin > 0 else 0.0,
        "deposits": deposits,
        "withdrawals": withdrawals,
        "trades": trades,
        "open_positions": open_all,
        "open_total": len(open_all),
        "open_bot": len(open_bot),
        "float_bot": sum(p.profit for p in open_bot),
        "float_manual": sum(p.profit for p in open_all if p.magic != magic),
    }, None