# core/stats.py
"""Pure performance statistics. No MT5, DB, Telegram or settings: callers pass the data in."""


def deal_stats(trades):
    """Win/loss, profit factor, expectancy, realized drawdown and streaks.
    trades: iterable of dicts with a numeric "net_pnl", in chronological order."""
    pnls = [t["net_pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    decided = len(wins) + len(losses)

    cum = peak = max_dd = 0.0
    cur_w = cur_l = max_w = max_l = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        if p > 0:
            cur_w, cur_l = cur_w + 1, 0
        elif p < 0:
            cur_l, cur_w = cur_l + 1, 0
        else:
            cur_w = cur_l = 0
        max_w, max_l = max(max_w, cur_w), max(max_l, cur_l)

    return {
        "n": len(pnls), "pnl": sum(pnls),
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / decided * 100, 1) if decided else 0.0,
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "expectancy": sum(pnls) / len(pnls) if pnls else 0.0,
        "best": max(pnls, default=0.0), "worst": min(pnls, default=0.0),
        "max_dd": max_dd, "max_w": max_w, "max_l": max_l,
    }


def signal_summary(counts):
    """Forward-test engine totals from {status: count} (core.db.get_signal_stats).
    SKIPPED (alert-only) signals are reported separately and never counted as results."""
    g = counts.get
    tp1, tp2, be, sl = g("CLOSED_TP1", 0), g("HIT_TP2", 0), g("CLOSED_BE", 0), g("HIT_SL", 0)
    wins = tp1 + tp2 + be
    closed = wins + sl
    return {
        "skipped": g("SKIPPED", 0),
        "total": sum(v for k, v in counts.items() if k != "SKIPPED"),
        "pending": g("PENDING", 0),
        "running": g("HIT_TP1", 0),          # leg 1 won, runner still open
        "tp1": tp1, "tp2": tp2, "be": be, "sl": sl,
        "wins": wins, "closed": closed,
        "win_rate": round(wins / closed * 100, 1) if closed else 0.0,
    }


def realized_return_pct(equity, deposits, withdrawals):
    """Account return on net deposits, in percent. None when nothing net was deposited."""
    net_deposits = deposits - withdrawals
    if net_deposits <= 0:
        return None
    return (equity - net_deposits) / net_deposits * 100